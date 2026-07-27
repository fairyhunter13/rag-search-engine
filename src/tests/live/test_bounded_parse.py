"""GPU-free functional tests for the HR39 bounded-parse worker pool (index/bounded_parse.py).

No cobol-grammar hang could be reproduced in this environment/tree-sitter-language-pack version
(exhaustive fuzzing attempted: raw + bundled parser, bytestring + read-callback source, 20+
pathological inputs — see session record). The timeout/kill/respawn mechanism is grammar-agnostic
by design (the pool bounds wall-clock time on ANY callable, not tree-sitter internals), so it is
proven here with a deterministic CPU-bound busy-loop — a closer analogue to a stuck native parse
than time.sleep, since it never yields to a signal-friendly blocking syscall. A separate parity
test proves the real cobol grammar round-trips correctly through the bounded path.
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.live


def _add(a: int, b: int) -> int:
    return a + b


def _boom() -> None:
    raise ValueError("worker-side failure")


def _busy_hang(seconds: float) -> str:
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        pass
    return "should never return before deadline"


def _unpicklable():
    import threading
    return threading.Lock()


def _new_pool():
    from rag_search.index.bounded_parse import BoundedParsePool
    return BoundedParsePool(size=2)


def _pid_alive(pid: int) -> bool:
    """Live and not a zombie. The orphan is reparented away from us, so waitpid is unavailable."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split(" ", 1)[0]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False
    return state != "Z"


# Holds a pool open and prints its worker pid, then blocks — so the test can SIGKILL it.
# The __main__ guard is load-bearing: spawn children re-import __main__, so without it the worker
# re-runs this script, dies on multiprocessing's bootstrap check, and BP1 goes green for entirely
# the wrong reason (observed — it passed against the unfixed worker in 0.39s).
_ORPHAN_PARENT = """
import time
from rag_search.index.bounded_parse import BoundedParsePool

if __name__ == "__main__":
    pool = BoundedParsePool(size=1)
    pool._ensure_started()
    print(next(iter(pool.pids)), flush=True)
    time.sleep(600)
"""


def test_bp1_worker_exits_when_its_parent_is_killed(tmp_path: Path) -> None:
    """A worker must not outlive its parent.

    `daemon=True` only reaps children through multiprocessing's `atexit`, which a SIGKILL'd (or
    SIGTERM'd) parent never runs; the old unbounded `task_q.get()` then blocked forever on a pipe
    with no writer. That leaked one worker per killed daemon or test run — 15 of them, 589 MB,
    were found alive on this box, the oldest 13 hours old. Red before the parent check.
    """
    script = tmp_path / "orphan_parent.py"
    script.write_text(_ORPHAN_PARENT)
    env = {**os.environ, "RSE_BOUNDED_PARSE_PARENT_CHECK_S": "1"}
    proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE,
                            env=env, text=True)
    worker_pid = -1
    try:
        worker_pid = int(proc.stdout.readline().strip())
        assert _pid_alive(worker_pid), "worker did not start"
        proc.kill()
        proc.wait(timeout=10)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and _pid_alive(worker_pid):
            time.sleep(0.25)
        assert not _pid_alive(worker_pid), (
            f"worker {worker_pid} outlived its killed parent — the orphan leak is back"
        )
    finally:
        if worker_pid > 0 and _pid_alive(worker_pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(worker_pid, 9)
        with contextlib.suppress(Exception):
            proc.kill()


def test_bp2_dead_slot_respawns_instead_of_burning_the_deadline() -> None:
    """A worker that exited must cost a respawn, not a full deadline of silence.

    Discriminating on both value and time: without the liveness check `run` posts into the dead
    worker's queue, waits the entire `deadline_s`, returns PARSE_TIMEOUT and inflates
    parse_timeout_count — and BP1's self-exit would otherwise make that the normal path after
    every idle period.
    """
    from rag_search.index.bounded_parse import BoundedParsePool
    pool = BoundedParsePool(size=1)
    try:
        pool._ensure_started()
        proc = pool._slots[0].proc
        os.kill(proc.pid, 9)
        proc.join(timeout=10)
        assert not proc.is_alive()
        t0 = time.monotonic()
        assert pool.run(_add, (7, 5), deadline_s=30) == 12
        assert time.monotonic() - t0 < 15, "respawn should be fast, not a burnt 30s deadline"
        assert pool.parse_timeout_count == 0, "a dead slot is not a parse timeout"
    finally:
        pool.idle_shutdown(idle_s=0)


def test_run_bounded_normal_execution() -> None:
    from rag_search.index.bounded_parse import PARSE_TIMEOUT, run_bounded
    result = run_bounded(_add, (2, 3), deadline_s=5)
    assert result == 5
    assert result != PARSE_TIMEOUT


def test_pool_timeout_kills_and_respawns_only_that_slot() -> None:
    pool = _new_pool()
    try:
        pool._ensure_started()
        before = pool.pids
        assert len(before) == 2
        t0 = time.time()
        result = pool.run(_busy_hang, (30,), deadline_s=1.5, path_for_log="/x/hang.cob")
        dt = time.time() - t0
        assert result == "PARSE_TIMEOUT"
        assert dt < 8, f"timeout took {dt}s, should bound near the 1.5s deadline"
        assert pool.parse_timeout_count == 1
        after = pool.pids
        assert len(after) == 2
        assert after != before, "the timed-out slot must be respawned with a new pid"
        assert len(after & before) == 1, "exactly one OTHER slot's pid must be unaffected"
    finally:
        pool.idle_shutdown(idle_s=0)


def test_pool_healthy_after_timeout() -> None:
    pool = _new_pool()
    try:
        pool.run(_busy_hang, (30,), deadline_s=1.0, path_for_log="/x/hang2.cob")
        assert pool.run(_add, (10, 20), deadline_s=5) == 30
    finally:
        pool.idle_shutdown(idle_s=0)


def test_worker_exception_returns_none_and_pool_stays_healthy() -> None:
    pool = _new_pool()
    try:
        assert pool.run(_boom, (), deadline_s=5, path_for_log="/x/boom.py") is None
        assert pool.run(_add, (1, 1), deadline_s=5) == 2
    finally:
        pool.idle_shutdown(idle_s=0)


def test_sigkill_mid_task_recovers() -> None:
    """Fault injection: SIGKILL a worker mid-task; the parent must recover and keep serving."""
    pool = _new_pool()
    try:
        pool._ensure_started()
        pid = pool._slots[0].proc.pid
        os.kill(pid, 9)
        time.sleep(0.3)
        assert pool.run(_busy_hang, (30,), deadline_s=1.0, path_for_log="/x/kill.cob") == "PARSE_TIMEOUT"
        assert pool.run(_add, (3, 4), deadline_s=5) == 7
    finally:
        pool.idle_shutdown(idle_s=0)


def test_idle_shutdown_frees_workers_when_no_task_in_flight() -> None:
    pool = _new_pool()
    pool.run(_add, (1, 2), deadline_s=5)
    assert pool._slots[0] is not None
    pool.idle_shutdown(idle_s=0)
    assert pool._slots[0] is None
    assert pool.run(_add, (5, 5), deadline_s=5) == 10
    pool.idle_shutdown(idle_s=0)


def test_metrics_reports_timeout_count() -> None:
    from rag_search.index import bounded_parse
    pool = _new_pool()
    bounded_parse._pool = pool
    try:
        pool.run(_busy_hang, (30,), deadline_s=1.0, path_for_log="/x/m.cob")
        assert bounded_parse.metrics()["parse_timeout_count"] >= 1
    finally:
        pool.idle_shutdown(idle_s=0)
        bounded_parse._pool = None


def test_cobol_grammar_parity_through_bounded_path() -> None:
    """Real cobol grammar (the historically pathological one) parses correctly when bounded."""
    from rag_search.graph.extractor import extract_symbols
    from rag_search.index.bounded_parse import PARSE_TIMEOUT, run_bounded
    snippet = "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. HELLO.\n"
    result = run_bounded(extract_symbols, (Path("hello.cob"), snippet, "cobol"),
                          deadline_s=10, path_for_log="hello.cob")
    assert result != PARSE_TIMEOUT
    assert isinstance(result, list)


def test_unpicklable_result_times_out_gracefully_not_a_crash() -> None:
    """An unpicklable worker return value surfaces as PARSE_TIMEOUT, never an unhandled crash."""
    pool = _new_pool()
    try:
        assert pool.run(_unpicklable, (), deadline_s=2, path_for_log="/x/unpicklable.py") == "PARSE_TIMEOUT"
        assert pool.run(_add, (1, 1), deadline_s=5) == 2
    finally:
        pool.idle_shutdown(idle_s=0)
