"""C-behaviour: reconcile cooperative-cancellation mid-pass (slow, GPU, no mocks, no real paths).

test_reconcile_pause_stops_mid_pass — pause mid-pass; indexed_count < N (faithful stop test)
"""
from __future__ import annotations

import threading
import time

import pytest

pytestmark = pytest.mark.live


@pytest.mark.slow
def test_reconcile_pause_stops_mid_pass(safe_tmp_path):
    """Cooperative-cancellation: pause mid-pass; fewer than N members must be indexed.

    Builds N=6 synthetic repos requiring a real embed index each. A background thread runs
    reconcile_projects(); the instant the first vector DB appears (provably mid-pass), we
    flip _PAUSED=True — the same state POST /api/sweeps/pause sets on the daemon. After join,
    assert indexed_count < N (loop-top guard stopped the pass).
    """
    import shutil

    from rag_search.core.config import ProjectEntry, project_vector_db
    from rag_search.core.registry import remove_project, upsert_project
    from rag_search.daemon import sweeps

    N = 6
    members = [safe_tmp_path / f"mid{i}" for i in range(N)]
    vdbs = []
    for m in members:
        m.mkdir()
        for j in range(4):
            (m / f"mod{j}.py").write_text(
                f"def func_{j}():\n    return {j}\n\ndef helper_{j}(x):\n    return x + {j}\n"
            )
        upsert_project(ProjectEntry(path=str(m), enabled=True))
        vdbs.append(project_vector_db(str(m)))

    t0 = time.monotonic()
    done = threading.Event()

    def _run():
        sweeps.reconcile_projects()
        done.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    paused_at: float | None = None
    while not done.is_set():
        if any(v.exists() for v in vdbs):
            sweeps._PAUSED = True
            paused_at = time.monotonic()
            break
        time.sleep(0.05)

    thread.join(timeout=300)
    indexed = sum(1 for v in vdbs if v.exists())

    try:
        assert paused_at is not None, (
            "reconcile finished before first vdb appeared — all members indexed before pause "
            "arrived; N too small or embed faster than poll granularity"
        )
        assert indexed < N, (
            f"pause mid-pass must stop before all {N} members are indexed; "
            f"got indexed={indexed}/{N} — loop-top _PAUSED guard missing from reconcile_projects()"
        )
        print(
            f"\n[PAUSE-MID] indexed={indexed}/{N}, "
            f"stop-latency={time.monotonic()-paused_at:.2f}s, total={time.monotonic()-t0:.1f}s"
        )
    finally:
        sweeps._PAUSED = False
        for m in members:
            remove_project(str(m))
            shutil.rmtree(m, ignore_errors=True)
