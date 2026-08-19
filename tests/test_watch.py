"""Routing, and the symlink trap that a naive test passes while broken."""

from __future__ import annotations

import queue
import time
from pathlib import Path

import pytest

from coderag import config, index, projcfg, registry, watch


@pytest.fixture(autouse=True)
def fresh_queue(monkeypatch):
    monkeypatch.setattr(index, "_queue", queue.Queue())
    monkeypatch.setattr(index, "_state", index.State())


def _jobs() -> list:
    out = []
    while not index._queue.empty():
        out.append(index._queue.get_nowait())
    return out


def _wait_for_job(timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not index._queue.empty():
            return index._queue.get_nowait()
        time.sleep(0.1)
    return None


# -------------------------------------------------------------------- routing


def test_a_nested_project_owns_its_own_files(tmp_path):
    """Longest match, not first. A federation member can live under its root's
    tree, and first-match hands its files to the wrong store."""
    outer = tmp_path / "outer"
    inner = outer / "vendor" / "inner"
    inner.mkdir(parents=True)

    assert watch._owner(inner / "a.py", [outer, inner]) == inner
    assert watch._owner(outer / "b.py", [outer, inner]) == outer


def test_a_path_under_no_watched_project_is_dropped(tmp_path):
    assert watch._owner(tmp_path / "elsewhere" / "a.py", [tmp_path / "watched"]) is None


def test_a_storm_becomes_one_job_per_project(tmp_path):
    """4,000 files from a `git checkout` across six members is six jobs; the
    content-hash diff inside each is the same walk it would have done anyway."""
    a, b = tmp_path / "a", tmp_path / "b"
    for path in (a, b):
        path.mkdir()
    cfgs = {a: projcfg.ProjectConfig(), b: projcfg.ProjectConfig()}
    batch = {("x", str(a / f"f{i}.py")) for i in range(200)}
    batch |= {("x", str(b / "one.py"))}

    watch._dispatch(batch, [a, b], cfgs)
    jobs = _jobs()

    assert len(jobs) == 2
    assert sum(len(j.paths) for j in jobs) == 201


def test_churn_in_an_excluded_directory_never_reaches_the_queue(tmp_path):
    """The watcher decides with the indexer's own predicate, so it cannot wake
    the indexer for files the indexer would then refuse."""
    project = tmp_path / "p"
    project.mkdir()
    cfg = projcfg.ProjectConfig(exclude=("build/*",))
    batch = {("x", str(project / "build" / "out.js")), ("x", str(project / "src" / "a.py"))}

    watch._dispatch(batch, [project], {project: cfg})

    assert [j.paths for j in _jobs()] == [["src/a.py"]]


def test_a_secret_file_is_dropped_by_the_watcher_too(tmp_path):
    """An indexed .env is a secret in a searchable store, and a watcher that
    forwards it hands the indexer a path it only refuses by luck."""
    project = tmp_path / "p"
    project.mkdir()
    batch = {("x", str(project / ".env")), ("x", str(project / "app.py"))}

    watch._dispatch(batch, [project], {project: projcfg.ProjectConfig()})

    assert [j.paths for j in _jobs()] == [["app.py"]]


# ---------------------------------------------------------------- real inotify


def test_a_write_through_a_symlink_still_reaches_the_queue(tmp_path, monkeypatch):
    """The trap, and the reason this test writes where it does.

    inotify does not traverse symlinks: watching the link yields nothing when
    the target changes, silently. This engine sidesteps it by registering every
    member under its resolved path -- so the assertion has to write through the
    symlink while the watcher is watching only the real path. A test that
    touches the real path directly passes with the whole mechanism broken.
    """
    monkeypatch.setattr(config, "WATCH_DEBOUNCE_MS", 100)
    member = tmp_path / "member"
    (member / "src").mkdir(parents=True)
    root = tmp_path / "root"
    root.mkdir()
    (root / "linked").symlink_to(member)
    registry.claim(member, direct=True)

    watch.start()
    try:
        time.sleep(1.0)  # the Rust watcher has to arm before the write
        (root / "linked" / "src" / "new.py").write_text("def added():\n    return 1\n")
        job = _wait_for_job()
    finally:
        watch.stop()

    assert job is not None, "a write through a symlink was not seen"
    assert job.project == member, "the job must name the resolved path, never the link"
    assert job.paths == ["src/new.py"]


def test_the_watcher_reports_whether_it_is_actually_running(tmp_path):
    registry.claim(tmp_path, direct=True)
    assert not watch.watching()
    watch.start()
    try:
        assert watch.watching()
    finally:
        watch.stop()
    assert not watch.watching()


def test_rearm_is_observable_so_the_tick_can_pick_up_a_new_project():
    watch.rearm()
    assert watch._rearm.is_set()
    watch._rearm.clear()


def test_paths_that_vanish_before_the_batch_is_read_do_not_crash(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    batch = {("x", "/outside/entirely.py"), ("x", str(project / "kept.py"))}

    watch._dispatch(batch, [project], {project: projcfg.ProjectConfig()})

    assert [j.paths for j in _jobs()] == [["kept.py"]]


def test_a_project_directory_that_is_gone_is_not_watched(tmp_path):
    registry.claim(tmp_path / "missing", direct=True)
    assert watch._roots() == []


def test_a_relative_path_is_computed_against_the_owning_project(tmp_path):
    project = tmp_path / "deep" / "nested" / "p"
    project.mkdir(parents=True)
    batch = {("x", str(project / "src" / "mod" / "a.py"))}

    watch._dispatch(batch, [project], {project: projcfg.ProjectConfig()})

    assert [j.paths for j in _jobs()] == [["src/mod/a.py"]]


def test_dispatch_names_its_reason_so_a_log_can_tell_producers_apart(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    watch._dispatch({("x", str(project / "a.py"))}, [project], {project: projcfg.ProjectConfig()})
    assert _jobs()[0].reason == "watch"


def test_an_empty_batch_enqueues_nothing(tmp_path):
    watch._dispatch(set(), [tmp_path], {tmp_path: projcfg.ProjectConfig()})
    assert _jobs() == []


def test_the_owner_helper_accepts_the_project_root_itself(tmp_path):
    assert watch._owner(Path(tmp_path), [tmp_path]) == tmp_path
