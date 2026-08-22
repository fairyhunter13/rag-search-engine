"""The queue, and the full pass against the real model.

Split on the GPU marker rather than on mocks: the queue mechanics touch no
model at all, and the full pass calls the real embedder on a three-file repo.
There is no third kind of test here.
"""

from __future__ import annotations

import queue
import time

import pytest

from coderag import config, index, projcfg, registry, store


@pytest.fixture(autouse=True)
def fresh_queue(monkeypatch):
    """The daemon has one queue for its whole life; a test must not inherit it."""
    monkeypatch.setattr(index, "_queue", queue.Queue())
    monkeypatch.setattr(index, "_state", index.State())
    monkeypatch.setattr(index, "_worker", None)


def _paths(project) -> list[str]:
    conn = store.connect(project)
    return sorted(r["path"] for r in conn.execute("SELECT path FROM files"))


# ------------------------------------------------------------------- the queue


def test_submit_returns_before_the_work_does():
    """The whole point of the background design: a producer never blocks.

    `submit` is asserted against wall clock rather than against a flag, because
    a `submit` that quietly awaited the build would still set every flag.
    """
    started = time.perf_counter()
    for n in range(50):
        # Distinct projects: identical whole-project submits collapse now, and
        # a depth of 1 would prove nothing about blocking.
        index.submit(f"/nonexistent/project-{n}")
    assert time.perf_counter() - started < 0.5
    assert index.status()["queue_depth"] == 50


def test_an_identical_whole_project_submit_collapses():
    for _ in range(50):
        index.submit("/nonexistent/project")
    assert index.status()["queue_depth"] == 1

    # Partials are never dropped, and they do not suppress each other.
    index.submit("/nonexistent/project", paths=["a.py"])
    index.submit("/nonexistent/project", paths=["b.py"])
    assert index.status()["queue_depth"] == 3

    # Dequeued is gone: the queue is the state, so the next full submit lands.
    while not index._queue.empty():
        index._queue.get_nowait()
    index.submit("/nonexistent/project")
    assert index.status()["queue_depth"] == 1


def test_status_reports_idle_when_nothing_is_queued():
    assert index.status()["state"] == "idle"


def test_a_failing_project_does_not_stop_the_queue(tmp_path):
    """One bad project among 148 must cost that project, not the run."""
    good = tmp_path / "good"
    good.mkdir()
    registry.claim(good, direct=True)

    index.submit("/nonexistent/project")
    index.submit(good)
    index.start_worker()
    index.stop_worker(timeout=30)

    assert index.status()["failed"] >= 1
    assert index.status()["completed"] >= 1


def test_a_failure_is_recorded_on_the_row_not_swallowed(tmp_path):
    missing = tmp_path / "gone"
    missing.mkdir()
    registry.claim(missing, direct=True)
    missing.rmdir()

    index.submit(missing)
    index.start_worker()
    index.stop_worker(timeout=30)

    entry = registry.get(missing)
    assert entry is not None and entry.last_error


def test_reconcile_enqueues_every_enabled_project_and_no_others(tmp_path):
    for name, enabled in (("a", True), ("b", True), ("c", False)):
        path = tmp_path / name
        path.mkdir()
        registry.claim(path, direct=True)
        if not enabled:
            registry.set_enabled(path, False)

    assert index.reconcile_all() == 2


def test_inherited_excludes_are_counted_not_inferred(repo, tmp_path):
    """`index` reports how much a root's excludes are suppressing.

    Both directions, or the number is satisfied by an empty project: with no
    root the count is zero, and with a root that excludes the js file it is one.
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / config.PROJECT_CONFIG_NAME).write_text('index:\n  exclude: ["*.js"]\n')

    assert index.suppressed_by_excludes(repo) == 0
    assert index.suppressed_by_excludes(repo, (str(root),)) == 1


def test_a_stored_language_is_re_derived_rather_than_left_stale(tmp_path):
    """The measured shape: adding `.groovy` to LANGS reclassified nothing,
    because the content-hash diff never rewrites a file that did not change."""
    project = tmp_path / "proj"
    project.mkdir()
    conn = store.connect(project)
    conn.execute(
        "INSERT INTO files (path, mtime, size, sha256, lang) VALUES (?, 0, 0, 'x', '')",
        ("build.groovy",),
    )
    conn.commit()

    assert index._relang(conn) == 1
    assert store.file_langs(conn)["build.groovy"] == "groovy"
    assert index._relang(conn) == 0


# ------------------------------------------------------- the pass, on real GPU


@pytest.mark.gpu
def test_one_pass_indexes_and_the_second_is_a_no_op(repo):
    first = index.index_project(repo)
    assert _paths(repo) == [".gitignore", "src/app.py", "src/util.js"]
    assert first["chunks"] >= 3 and first["written"] == 3

    second = index.index_project(repo)
    assert second["written"] == 0 and second["deleted"] == 0
    assert (second["files"], second["chunks"]) == (first["files"], first["chunks"])


@pytest.mark.gpu
def test_the_diff_notices_an_edit_and_a_delete(repo):
    index.index_project(repo)

    (repo / "src" / "app.py").write_text("def renamed():\n    return 1\n")
    (repo / "src" / "util.js").unlink()
    result = index.index_project(repo)

    assert result["written"] == 1 and result["deleted"] >= 1
    assert _paths(repo) == [".gitignore", "src/app.py"]


@pytest.mark.gpu
def test_a_scoped_pass_removes_the_file_the_watcher_named(repo):
    """The watcher's own call shape, which the full-walk test above does not
    reach: it passes `paths`, so the content-hash diff never runs and the
    delete list is computed from the named paths alone."""
    index.index_project(repo)

    (repo / "src" / "util.js").unlink()
    result = index.index_project(repo, ["src/util.js"])

    assert result["deleted"] >= 1
    assert "src/util.js" not in _paths(repo)


@pytest.mark.gpu
def test_a_root_joining_shrinks_an_existing_index_without_a_rebuild(repo, tmp_path):
    """The late-join sequence, which is the common case and not the edge case.

    Index standalone, confirm a hit on a path the root excludes, then let the
    root claim it. The `config_signature` mismatch has to turn the next pass
    into a reconcile that *removes* what the new config excludes -- otherwise
    the project keeps serving what its root spent 70.9% of an index excluding,
    and nothing anywhere notices.
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / config.PROJECT_CONFIG_NAME).write_text('index:\n  exclude: ["*.js"]\n')
    registry.claim(repo, direct=True)

    index.index_project(repo)
    assert "src/util.js" in _paths(repo)

    registry.claim(repo, root=root)
    index.index_project(repo)

    assert "src/util.js" not in _paths(repo), "the root's excludes must reach an older member"
    assert "src/app.py" in _paths(repo), "and must not take the first-party source with them"


@pytest.mark.gpu
def test_leaving_the_root_widens_it_back(repo, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / config.PROJECT_CONFIG_NAME).write_text('index:\n  exclude: ["*.js"]\n')
    registry.claim(repo, direct=True)
    registry.claim(repo, root=root)
    index.index_project(repo)
    assert "src/util.js" not in _paths(repo)

    registry.release(repo, root=root)
    index.index_project(repo)
    assert "src/util.js" in _paths(repo)


@pytest.mark.gpu
def test_a_changed_chunker_setting_rebuilds_rather_than_mixing(repo, monkeypatch):
    """Two chunk budgets in one table is not an error SQLite can see."""
    index.index_project(repo)
    conn = store.connect(repo)
    monkeypatch.setattr(config, "CHUNK_CHARS", 120)

    assert store.incompatible(conn)
    index.index_project(repo)
    assert store.incompatible(conn) is None
    assert store.orphans(conn) == {"fts": 0, "vec": 0}


@pytest.mark.gpu
def test_a_targeted_pass_touches_only_the_named_files(repo):
    index.index_project(repo)
    (repo / "src" / "app.py").write_text("def only_this_one():\n    return 2\n")
    (repo / "src" / "util.js").write_text("export function alsoChanged() {}\n")

    result = index.index_project(repo, paths=["src/app.py"])
    assert result["written"] == 1


@pytest.mark.gpu
def test_the_signature_is_stamped_so_a_restart_does_not_re_walk(repo):
    registry.claim(repo, direct=True)
    index.index_project(repo)
    conn = store.connect(repo)
    assert store.get_meta(conn, "config_signature") == projcfg.effective(repo, ()).signature()

    entry = registry.get(repo)
    assert entry is not None and entry.chunk_count > 0 and entry.last_error is None
