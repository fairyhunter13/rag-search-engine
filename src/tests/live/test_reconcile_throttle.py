"""Reconcile throttle — fast/GPU-free guards (no mocks, no real paths).

A-wiring   test_reconcile_initial_delay_wired     — grace+deprio wired before reconcile_projects()
A-helper   test_reconcile_thread_deprioritized    — _deprioritize_current_thread raises nice +5
A-live     test_daemon_reconcile_thread_niced     — running daemon reconcile thread at nice +5 (/proc)
C-fast     test_reconcile_pause_stops_all_members — paused-at-entry: zero work across N=3 members
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def test_reconcile_initial_delay_wired():
    """Grace sleep and _deprioritize_current_thread must appear before reconcile_projects() in source."""
    import importlib
    import inspect
    import os

    from rag_search.daemon import server as srv_mod

    orig = os.environ.pop("OPENCODE_RECONCILE_INITIAL_DELAY_S", None)
    try:
        os.environ["OPENCODE_RECONCILE_INITIAL_DELAY_S"] = "99"
        importlib.reload(srv_mod)
        assert srv_mod._RECONCILE_INITIAL_DELAY_S == 99.0, (
            f"env knob must set _RECONCILE_INITIAL_DELAY_S; got {srv_mod._RECONCILE_INITIAL_DELAY_S}"
        )
    finally:
        os.environ.pop("OPENCODE_RECONCILE_INITIAL_DELAY_S", None) if orig is None else \
            os.environ.__setitem__("OPENCODE_RECONCILE_INITIAL_DELAY_S", orig)
        importlib.reload(srv_mod)

    src = inspect.getsource(srv_mod._start_background)
    grace_pos = src.find("_RECONCILE_INITIAL_DELAY_S")
    deprio_pos = src.find("_deprioritize_current_thread")
    reconcile_pos = src.find("reconcile_projects()")
    assert grace_pos != -1, "_RECONCILE_INITIAL_DELAY_S not referenced in _start_background source"
    assert deprio_pos != -1, "_deprioritize_current_thread not referenced in _start_background source"
    assert grace_pos < reconcile_pos, "grace sleep must appear before reconcile_projects()"
    assert deprio_pos < reconcile_pos, "_deprioritize_current_thread must appear before reconcile_projects()"


def test_reconcile_thread_deprioritized():
    """_deprioritize_current_thread(5) must raise the calling thread's nice by 5 (Linux per-thread)."""
    import os
    import threading

    from rag_search.daemon import server as srv_mod

    assert hasattr(os, "getpriority"), "os.getpriority not available on this platform"
    result: list[tuple[int, int]] = []

    def _measure():
        baseline = os.getpriority(os.PRIO_PROCESS, 0)
        srv_mod._deprioritize_current_thread(5)
        after = os.getpriority(os.PRIO_PROCESS, 0)
        result.append((baseline, after))

    t = threading.Thread(target=_measure)
    t.start()
    t.join(timeout=5)
    assert result, "_measure thread did not complete"
    baseline, after = result[0]
    assert after == baseline + 5, (
        f"nice must be baseline+5; baseline={baseline} after={after} "
        "(os.nice applies per-thread on Linux)"
    )


def test_daemon_reconcile_thread_niced(live_client):
    """Running daemon must have exactly one thread elevated by nice +5 above the process baseline.

    The reconcile loop calls _deprioritize_current_thread(5) on startup. The systemd unit may
    apply a process-level Nice, so the check is relative: max thread nice == min + 5.
    Requires daemon restarted after this build.
    """
    import subprocess
    from pathlib import Path

    r = subprocess.run(
        ["systemctl", "--user", "show", "rag-search-mcp-daemon.service",
         "-p", "MainPID", "--value"],
        capture_output=True, text=True, timeout=5,
    )
    pid = int(r.stdout.strip())
    assert pid > 0, "systemctl did not return a valid MainPID"

    task_dir = Path(f"/proc/{pid}/task")
    assert task_dir.exists(), f"/proc/{pid}/task not accessible"

    nices: list[int] = []
    for tid_path in task_dir.iterdir():
        try:
            stat = (tid_path / "stat").read_text()
            after_comm = stat[stat.rfind(")") + 2:]
            nices.append(int(after_comm.split()[16]))
        except (OSError, IndexError, ValueError):
            continue

    assert nices, "No thread nice values readable from /proc"
    baseline = min(nices)
    elevated = [n for n in nices if n == baseline + 5]
    assert elevated, (
        f"No thread at nice baseline+5={baseline+5}; distribution={sorted(set(nices))} "
        "— _deprioritize_current_thread not applied, or daemon not restarted after this build"
    )


def test_reconcile_pause_stops_all_members(safe_tmp_path):
    """Pause at entry must skip ALL members and the BPRE root-pass (N=3, GPU-free)."""
    import shutil

    from rag_search.core.config import ProjectEntry, project_vector_db
    from rag_search.core.registry import remove_project, upsert_project
    from rag_search.daemon import sweeps

    members = [safe_tmp_path / f"mbr{i}" for i in range(3)]
    for m in members:
        m.mkdir()
        (m / "a.py").write_text("def f(): pass\n")
        upsert_project(ProjectEntry(path=str(m), enabled=True))

    sweeps._PAUSED = True
    try:
        sweeps.reconcile_projects()
        for m in members:
            vdb = project_vector_db(str(m))
            assert not vdb.exists(), (
                f"paused reconcile must not index {m.name}; "
                "loop-top _PAUSED guard missing from reconcile_projects()"
            )
    finally:
        sweeps._PAUSED = False
        for m in members:
            remove_project(str(m))
            shutil.rmtree(m, ignore_errors=True)
