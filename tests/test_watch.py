"""Routing, and the symlink trap that a naive test passes while broken."""

from __future__ import annotations

import queue
import threading
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


def test_a_delete_through_a_symlink_still_reaches_the_queue(tmp_path, monkeypatch):
    """The write direction had a test and the delete direction had none, which
    is the direction that fails silently: an index that only ever adds keeps
    answering with line ranges that no longer exist. The file is created before
    the watcher starts, so the only event this can pass on is the removal."""
    monkeypatch.setattr(config, "WATCH_DEBOUNCE_MS", 100)
    member = tmp_path / "member"
    (member / "src").mkdir(parents=True)
    (member / "src" / "gone.py").write_text("def gone():\n    return 1\n")
    root = tmp_path / "root"
    root.mkdir()
    (root / "linked").symlink_to(member)
    registry.claim(member, direct=True)

    watch.start()
    try:
        time.sleep(1.0)
        (root / "linked" / "src" / "gone.py").unlink()
        job = _wait_for_job()
    finally:
        watch.stop()

    assert job is not None, "a delete through a symlink was not seen"
    assert job.project == member
    assert job.paths == ["src/gone.py"]


def test_the_watcher_reports_whether_it_is_actually_running(tmp_path):
    registry.claim(tmp_path, direct=True)
    assert not watch.watching()
    watch.start()
    try:
        assert watch.watching()
    finally:
        watch.stop()
    assert not watch.watching()


def test_one_broken_config_does_not_take_the_whole_watcher_down(tmp_path, monkeypatch):
    """A `ConfigError` raised while building the per-project config escaped the
    thread. Nothing restarts a watcher and `watching()` was the only thing that
    would have said so, so every other repo in the fleet quietly stopped
    noticing writes because one of them had a typo."""
    broken = tmp_path / "broken"
    good = tmp_path / "good"
    for path in (broken, good):
        path.mkdir()
        registry.claim(path, direct=True)
    (broken / ".coderag.toml").write_text('[index]\nexcludes = ["x/*"]\n')

    monkeypatch.setattr(config, "WATCH_DEBOUNCE_MS", 100)
    watch.start()
    try:
        time.sleep(1.0)  # the Rust watcher has to arm before the write
        (good / "a.py").write_text("x = 1\n")
        job = _wait_for_job()
        assert watch.watching(), "the watcher thread died on the broken project"
        assert job is not None and job.project == good, job
    finally:
        watch.stop()


def test_the_tick_restarts_a_watcher_that_died(monkeypatch):
    """`rearm` only sets a flag, and a dead thread reads no flags."""
    from coderag import server

    started = []
    monkeypatch.setattr(watch, "start", lambda: started.append(1))
    monkeypatch.setattr(watch, "rearm", lambda: None)
    monkeypatch.setattr(server.config, "SCHEDULER_TICK_S", 0.01)
    monkeypatch.setattr(server.config, "MODEL_IDLE_UNLOAD_S", 0)
    thread = threading.Thread(target=server._tick, daemon=True)
    server._stop.clear()
    thread.start()
    time.sleep(0.15)
    server._stop.set()
    thread.join(2)
    assert started, "the tick never restarted the watcher"


def test_an_event_reported_through_a_roots_symlink_goes_to_the_member(tmp_path):
    """The path notify reports is not the path the file lives at.

    inotify keys a directory by inode, so a root and the member it reaches
    through a symlink share one watch descriptor and the reported path is
    whichever was registered last -- measured both ways against the real
    watcher, and nothing here chooses the order. The batch is synthetic for
    that reason: driving this through notify tests the ordering, not the fix.
    """
    member, root = tmp_path / "m", tmp_path / "r"
    (member / "src").mkdir(parents=True)
    root.mkdir()
    (root / "link").symlink_to(member, target_is_directory=True)
    for path in (member, root):
        registry.claim(path, direct=True)

    roots = [member, root]
    configs = dict.fromkeys(roots, projcfg.ProjectConfig())
    watch._dispatch([("2", str(root / "link" / "src" / "a.py"))], roots, configs)

    jobs = _jobs()
    assert jobs and jobs[0].project == member, (
        f"the member's own write was billed to {jobs and jobs[0].project}"
    )
    assert jobs[0].paths == ["src/a.py"], jobs[0].paths


def test_the_tick_does_not_rearm_a_watch_set_that_has_not_changed(tmp_path, monkeypatch):
    """The blind window, and why it is not a tuning knob.

    Re-arming rebuilds every inotify watch -- 5.4 s over the real 151-project
    fleet -- and inotify replays nothing, so every re-arm is a window in which
    a delete is lost permanently. Done unconditionally on the 60 s tick, that
    was a tenth of the watcher's life, and it is what kept a deleted file
    searchable through three 60 s polls in a row.

    The assertion is in two halves because either alone passes on the broken
    code: unconditional `rearm` passes the second, a `pass` passes the first.
    """
    monkeypatch.setattr(watch, "_armed", tuple(watch._roots()))
    watch._rearm.clear()
    watch.rearm_if_changed()
    assert not watch._rearm.is_set(), "an unchanged registry re-armed the watcher"

    project = tmp_path / "newcomer"
    project.mkdir()
    registry.claim(project, direct=True)
    watch.rearm_if_changed()
    assert watch._rearm.is_set(), "a newly registered project was never picked up"


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
