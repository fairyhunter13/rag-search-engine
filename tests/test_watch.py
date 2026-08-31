"""Routing, and the symlink trap that a naive test passes while broken."""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from pathlib import Path

import pytest

from coderag import config, index, projcfg, quiet, registry, tools, watch


@pytest.fixture(autouse=True)
def fresh_queue(monkeypatch):
    monkeypatch.setattr(index, "_queue", queue.Queue())
    monkeypatch.setattr(index, "_state", index.State())
    quiet.flush()
    yield
    quiet.flush()


def _promote() -> None:
    """A watch job waits out `WATCH_QUIET_MS`. Routing is what these assert."""
    for project, paths, reason in quiet.flush():
        index.submit(project, paths, reason=reason)


def _jobs() -> list:
    _promote()
    out = []
    while not index._queue.empty():
        out.append(index._queue.get_nowait())
    return out


def _wait_for_job(timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _promote()
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


def test_a_batch_is_held_for_the_quiet_window_and_not_queued(tmp_path):
    """303 passes in 15 minutes, because a batch carries one event."""
    project = tmp_path / "p"
    project.mkdir()

    watch._dispatch({("x", str(project / "a.py"))}, [project], {project: projcfg.ProjectConfig()})

    assert index._queue.empty()
    assert quiet.pending() == 1


def test_churn_in_an_excluded_directory_never_reaches_the_queue(tmp_path):
    """The watcher decides with the indexer's own predicate, so it cannot wake
    the indexer for files the indexer would then refuse."""
    project = tmp_path / "p"
    project.mkdir()
    cfg = projcfg.ProjectConfig(exclude=("build/*",))
    batch = {("x", str(project / "build" / "out.js")), ("x", str(project / "src" / "a.py"))}

    watch._dispatch(batch, [project], {project: cfg})

    assert [j.paths for j in _jobs()] == [["src/a.py"]]


def test_a_gitignored_build_cache_never_reaches_the_queue(tmp_path):
    """The measured loop: a turbo cache, gitignored and so never indexed, woke
    the indexer for a full-project diff every few seconds across the fleet.
    `indexable` alone cannot see this -- only `git ls-files` did."""
    project = tmp_path / "p"
    (project / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / ".gitignore").write_text(".turbo\n", encoding="utf-8")
    batch = {
        ("x", str(project / ".turbo" / "cache" / "a-meta.json")),
        ("x", str(project / "src" / "a.py")),
    }

    watch._dispatch(batch, [project], {project: projcfg.ProjectConfig()})

    assert [j.paths for j in _jobs()] == [["src/a.py"]], "the tracked write must still wake it"


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


def test_armed_waits_for_the_first_yield_rather_than_the_decision_to_rebuild(tmp_path, monkeypatch):
    """The blind window is the rebuild itself: establishing ~120,000 watches
    takes seconds, and `armed` used to flip before the first watch existed --
    so a fixture that waited on it wrote into exactly the window it was
    avoiding."""
    registry.claim(tmp_path, direct=True)
    started = threading.Event()

    def _slow_watch(*roots, **kw):
        started.set()
        time.sleep(0.5)
        yield set()
        while not watch._stop.is_set():
            time.sleep(0.05)

    monkeypatch.setattr(watch, "_watch", _slow_watch)
    watch.start()
    try:
        assert started.wait(5)
        assert not watch.armed(tmp_path), "armed before a single watch existed"
        assert _until(lambda: watch.armed(tmp_path)), "never armed after the first yield"
    finally:
        watch.stop()


def _until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_a_live_thread_is_not_the_same_answer_as_this_project_being_watched(tmp_path, monkeypatch):
    """What `index` reports as `watching`. A registered project is not yet an
    armed one, and a write in between is lost -- so the discriminator is that
    the thread is alive for both halves and the answer still changes."""
    # The real loop owns `_armed` and this test writes it, so running it here is
    # a data race the assertions lose: `isolated_state` puts the registry under
    # `tmp_path`, so claiming `tmp_path` had the watcher arming off the test's
    # own writes. `_loop` under the real loop is `test_one_broken_config_...`.
    monkeypatch.setattr(watch, "_loop", lambda: watch._stop.wait(30))
    registry.claim(tmp_path, direct=True)
    watch.start()
    try:
        assert watch.watching()
        assert not watch.armed(tmp_path)
        watch._armed = tuple(watch._roots())
        assert watch.armed(tmp_path)
    finally:
        watch.stop()


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
    (broken / config.PROJECT_CONFIG_NAME).write_text('index:\n  excludes: ["x/*"]\n')

    monkeypatch.setattr(config, "WATCH_DEBOUNCE_MS", 100)
    watch.start()
    try:
        time.sleep(1.0)  # the Rust watcher has to arm before the write
        (good / "a.py").write_text("x = 1\n")
        job = _wait_for_job()
        assert watch.watching(), "the watcher thread died on the broken project"
        assert job is not None and job.project == good, job
        # The flag has to name what the watcher actually watches: a caller that
        # waits on `armed` and then writes is told a dropped project is covered.
        assert watch.armed(good)
        assert not watch.armed(broken)
    finally:
        watch.stop()


def test_the_tick_restarts_a_watcher_that_died(monkeypatch):
    """A re-arm only sets a flag, and a dead thread reads no flags."""
    from coderag import server

    started = []
    monkeypatch.setattr(watch, "start", lambda: started.append(1))
    monkeypatch.setattr(watch, "rearm_if_changed", lambda: None)
    monkeypatch.setattr(server.config, "SCHEDULER_TICK_S", 0.01)
    monkeypatch.setattr(server.config, "MODEL_IDLE_UNLOAD_S", 0)
    thread = threading.Thread(target=server._tick, daemon=True)
    server._stop.clear()
    thread.start()
    time.sleep(0.15)
    server._stop.set()
    thread.join(2)
    assert started, "the tick never restarted the watcher"


def test_the_tick_sweeps_on_its_own_counter_and_not_every_tick(monkeypatch):
    """The sweep branch ran in no test. `_tick`'s only exercise left
    `SWEEP_EVERY_S` at an hour, so deleting the branch outright kept the suite
    green -- and the fast arm alone passes against an unconditional sweep."""
    from coderag import server

    def ticks_with(sweep_every: float) -> list[int]:
        swept: list[int] = []
        monkeypatch.setattr(server, "_sweep", lambda: swept.append(1))
        monkeypatch.setattr(watch, "start", lambda: None)
        monkeypatch.setattr(watch, "rearm_if_changed", lambda: None)
        monkeypatch.setattr(server.config, "SCHEDULER_TICK_S", 0.01)
        monkeypatch.setattr(server.config, "SWEEP_EVERY_S", sweep_every)
        monkeypatch.setattr(server.config, "MODEL_IDLE_UNLOAD_S", 0)
        thread = threading.Thread(target=server._tick, daemon=True)
        server._stop.clear()
        thread.start()
        time.sleep(0.15)
        server._stop.set()
        thread.join(2)
        return swept

    assert ticks_with(0.01), "the counter fired and nothing swept"
    assert not ticks_with(3600), "a tenth of a second swept at the default hour"


def test_the_sweep_reconciles_what_it_claimed(monkeypatch):
    """A member claimed and never reconciled is a row with no store behind it,
    which reads as an empty project rather than as an error."""
    from coderag import server

    calls: list[str] = []
    monkeypatch.setattr(server.federation, "sweep", lambda: (calls.append("sweep"), ([], []))[1])
    monkeypatch.setattr(server.index, "reconcile_all", lambda: calls.append("reconcile"))
    monkeypatch.setattr(server.watch, "rearm_if_changed", lambda: calls.append("rearm"))

    server._sweep()

    assert calls == ["sweep", "reconcile", "rearm"], calls


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
    monkeypatch.setattr(watch, "_intent", watch._watch_set())
    watch._rearm.clear()
    watch.rearm_if_changed()
    assert not watch._rearm.is_set(), "an unchanged registry re-armed the watcher"

    project = tmp_path / "newcomer"
    project.mkdir()
    registry.claim(project, direct=True)
    watch.rearm_if_changed()
    assert watch._rearm.is_set(), "a newly registered project was never picked up"


def test_repairing_a_config_rearms_the_project_it_unwatched(tmp_path, monkeypatch):
    """The repair is invisible to the registry, and the registry was the whole
    comparison. A project the loop drops for a broken config stays enabled, so
    fixing the file re-armed nothing and that project stayed unwatched until the
    daemon restarted -- the hourly sweep kept its store fresh, which is why this
    reads as healthy everywhere it is asked."""
    project = tmp_path / "typo"
    project.mkdir()
    registry.claim(project, direct=True)
    (project / config.PROJECT_CONFIG_NAME).write_text("exclude: [oops\n")
    monkeypatch.setattr(watch, "_intent", watch._watch_set())
    watch._rearm.clear()

    (project / config.PROJECT_CONFIG_NAME).write_text("exclude: [ok]\n")
    os.utime(project / config.PROJECT_CONFIG_NAME, (2e9, 2e9))
    watch.rearm_if_changed()

    assert watch._rearm.is_set(), "the repaired project was never picked up again"


def test_the_retired_config_name_is_stamped_too(tmp_path, monkeypatch):
    """`projcfg` refuses a leftover `.coderag.toml` rather than ignoring it, so
    it drops the project exactly as a broken YAML file does. Deleting it is the
    repair, and it moves no other file."""
    project = tmp_path / "retired"
    project.mkdir()
    registry.claim(project, direct=True)
    (project / config.RETIRED_CONFIG_NAME).write_text("[index]\n")
    monkeypatch.setattr(watch, "_intent", watch._watch_set())
    watch._rearm.clear()

    (project / config.RETIRED_CONFIG_NAME).unlink()
    watch.rearm_if_changed()

    assert watch._rearm.is_set(), "removing the retired config re-armed nothing"


def test_an_oserror_out_of_the_watcher_is_recorded_rather_than_fatal(tmp_path, monkeypatch):
    """inotify raises here at the per-user watch ceiling. Uncaught it killed the
    thread, and nothing covers this one: `server._guarded` wraps the scheduler
    thread, not this one. So the fleet stopped noticing writes, `watching` read
    False with no reason attached, and the only recovery was the 60 s respawn."""
    project = tmp_path / "p"
    project.mkdir()
    registry.claim(project, direct=True)

    def raising(*_a, **_k):
        watch._stop.set()
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(watch, "_watch", raising)
    watch._stop.clear()
    watch._loop()

    assert watch.error() is not None
    assert "No space left" in watch.error()
    assert not watch.armed(project), "a failed pass still reported itself armed"


def test_the_loop_records_what_it_armed_so_the_tick_can_compare(tmp_path):
    """Both re-arm tests set `_intent` by hand, so dropping the loop's own
    assignment leaves them green while the watcher re-arms every tick forever."""
    project = tmp_path / "watched"
    project.mkdir()
    registry.claim(project, direct=True)
    watch.start()
    try:
        deadline = time.time() + 15
        while not watch.armed(project) and time.time() < deadline:
            time.sleep(0.1)
        assert watch.armed(project), "the watcher never armed the one project it has"
        watch._rearm.clear()
        watch.rearm_if_changed()
        assert not watch._rearm.is_set(), "the loop left nothing for the tick to compare"
    finally:
        watch.stop()


def test_the_index_tool_does_not_rearm_a_registry_it_did_not_change(tmp_path, monkeypatch, pin):
    """The 300 s delete failure, at the layer that caused it.

    `index` used to re-arm on every call. A caller polling it -- every live
    test, and any agent watching a build -- held the watcher inside the blind
    window more or less continuously, and inotify replays nothing. So the two
    halves here are the whole defect: a repeat call must not re-arm, and a call
    that registers something must.
    """
    project = tmp_path / "repeat"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    monkeypatch.setattr(index, "submit", lambda *_a, **_k: None)
    monkeypatch.setattr(watch, "start", lambda: None)
    monkeypatch.setattr(index, "start_worker", lambda: None)

    workspace = pin(tmp_path)
    tools.index_project(workspace, root=str(project))
    monkeypatch.setattr(watch, "_intent", watch._watch_set())
    watch._rearm.clear()

    tools.index_project(workspace, root=str(project))
    assert not watch._rearm.is_set(), "a repeat index call re-armed the whole fleet"

    newcomer = tmp_path / "newcomer"
    newcomer.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=newcomer, check=True)
    tools.index_project(workspace, root=str(newcomer))
    assert watch._rearm.is_set(), "a new registration was never picked up"


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


def test_an_empty_batch_records_nothing_either(tmp_path):
    """A timeout yield is the common case. A row per one of those is the volume
    the dated refusal ruled out."""
    from coderag import runledger

    watch._dispatch(set(), [tmp_path], {tmp_path: projcfg.ProjectConfig()})
    assert runledger.read(kind="watch") == []


def test_a_dropped_event_says_which_of_the_five_answers_applies(tmp_path):
    """"I edited a file and it is not searchable" had five answers, and each
    drop was a bare `continue`, so the daemon told none of them apart."""
    from coderag import runledger

    project = tmp_path / "p"
    (project / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / ".gitignore").write_text("src/gen.py\n", encoding="utf-8")
    batch = {
        ("x", str(tmp_path / "elsewhere" / "a.py")),
        ("x", str(project / ".env")),
        ("x", str(project / "src" / "gen.py")),
        ("x", str(project / "src" / "a.py")),
    }

    watch._dispatch(batch, [project], {project: projcfg.ProjectConfig()})

    row = runledger.read(kind="watch")[0]
    assert row["raw"] == 4
    assert row["unowned"] == 1, row
    assert row["filtered"] == 1, row
    assert row["gitignored"] == 1, row
    assert row["submitted"] == {str(project): 1}, row


def test_the_owner_helper_accepts_the_project_root_itself(tmp_path):
    assert watch._owner(Path(tmp_path), [tmp_path]) == tmp_path
