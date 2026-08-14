"""Bounds every tree-sitter parse behind a persistent spawn-context worker pool (HR39).

Proven this session: tree-sitter 0.25's `progress_callback` never fires during cobol's
error-recovery loop — nothing in-process can cancel a stuck parse; subprocess isolation is the
only hard bound. Workers use `spawn` (never `fork` — the daemon holds a CUDA context + many
threads) and are CPU-only: never import the embedder/CUDA. `run_bounded(func, args, deadline_s)`
dispatches an existing extraction function (`extract_symbols`, `scan_file`, ...) into the pool;
its tree is created and consumed entirely inside the worker (Nodes aren't picklable) — only the
already-picklable return value crosses back. On timeout the pool terminates + respawns only that
one slot and bumps `parse_timeout_count`; the file is recorded (path-hash — HR34), not skipped.
"""
from __future__ import annotations
import contextlib, hashlib, logging, multiprocessing as mp, os, queue, threading, time
from dataclasses import dataclass
log = logging.getLogger(__name__)

PARSE_TIMEOUT = "PARSE_TIMEOUT"  # sentinel result, distinct from any real return value
PARSE_CRASHED = "PARSE_CRASHED"  # worker died mid-task — NOT the same event as a timeout
_DEADLINE_S = float(os.environ.get("RSE_BOUNDED_PARSE_DEADLINE_S", "10"))
_POLL_S = 0.25  # how often the wait re-checks whether the worker is still alive
_POOL_SIZE = int(os.environ.get("RSE_BOUNDED_PARSE_WORKERS", "1"))  # HR40: 1-core quota
_IDLE_SHUTDOWN_S = float(os.environ.get("RSE_BOUNDED_PARSE_IDLE_S", "120"))
_PARENT_CHECK_S = float(os.environ.get("RSE_BOUNDED_PARSE_PARENT_CHECK_S", "30"))
_CTX = mp.get_context("spawn")


def _path_hash(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()[:12]  # non-reversible log id, never the real path (HR34)


def _worker_main(task_q, result_q, parent_pid: int) -> None:
    """Persistent worker loop. CPU-only — must never import the embedder/CUDA.

    Wakes every `_PARENT_CHECK_S` to notice a vanished parent. `daemon=True` only reaps children
    through multiprocessing's `atexit`, which a SIGTERM'd or SIGKILL'd parent never runs, so an
    unbounded `get()` here leaks a worker per killed daemon or test run — it then blocks forever
    on a pipe whose writer is gone.

    `parent_pid` is passed in rather than read here: a spawn worker takes a second or two to boot,
    and if the parent dies inside that window `os.getppid()` already reads the reaper, so the
    worker would compare the reaper against itself and never exit (measured — py-spy showed
    `parent: 2316`). Comparing against the reaper pid directly is wrong for the same reason under
    `systemd --user`, a child subreaper: orphans land on the manager, never on init.
    """
    while True:
        try:
            item = task_q.get(timeout=_PARENT_CHECK_S)
        except queue.Empty:
            if os.getppid() != parent_pid:
                return
            continue
        if item is None:
            return
        func, args = item
        try:
            result_q.put(("ok", func(*args)))
        except Exception as exc:
            result_q.put(("error", repr(exc)))


@dataclass
class _Slot:
    proc: object; task_q: object; result_q: object


class BoundedParsePool:
    """Persistent spawn-context slots; on timeout only that slot is killed + respawned."""

    def __init__(self, size: int = _POOL_SIZE):
        self._size = max(1, size)
        self._lock = threading.Lock()
        self._free: queue.Queue = queue.Queue()
        self._slots: list[_Slot | None] = [None] * self._size
        self.parse_timeout_count = 0
        self.parse_crash_count = 0
        self._last_used = time.monotonic()

    def _ensure_started(self) -> None:
        if self._slots[0] is not None:
            return
        with self._lock:
            if self._slots[0] is None:
                for i in range(self._size):
                    self._spawn_slot(i); self._free.put(i)

    def _spawn_slot(self, i: int) -> None:
        task_q, result_q = _CTX.Queue(), _CTX.Queue()
        proc = _CTX.Process(target=_worker_main, args=(task_q, result_q, os.getpid()), daemon=True)
        proc.start()
        self._slots[i] = _Slot(proc, task_q, result_q)

    @property
    def pids(self) -> set[int]:
        return {s.proc.pid for s in self._slots if s is not None and s.proc.pid is not None}

    def run(self, func, args: tuple, deadline_s: float = _DEADLINE_S, path_for_log: str = ""):
        self._ensure_started()
        self._last_used = time.monotonic()
        idx = self._free.get()
        try:
            slot = self._slots[idx]
            if not slot.proc.is_alive():
                # Self-exited on the orphan check, or killed out from under us. Without this the
                # only symptom is a full `deadline_s` of silence and a phantom PARSE_TIMEOUT,
                # which also inflates parse_timeout_count.
                with self._lock:
                    self._spawn_slot(idx)
                slot = self._slots[idx]
            slot.task_q.put((func, args))
            got = self._await_result(slot, idx, deadline_s, path_for_log)
            if got is PARSE_TIMEOUT or got is PARSE_CRASHED:
                return got
            status, payload = got
            if status == "error":
                log.warning("bounded_parse worker error path_hash=%s: %s",
                            _path_hash(path_for_log), payload)
                return None
            return payload
        finally:
            self._free.put(idx)

    def _await_result(self, slot, idx: int, deadline_s: float, path_for_log: str):
        """Wait for a result, distinguishing a worker that *died* from one that ran too long.

        A single blocking `get(timeout=deadline_s)` cannot tell those apart: a worker killed by a
        signal never puts anything on the queue, so the wait runs to the full deadline and reports
        `PARSE_TIMEOUT`. That is not a pedantic distinction — a crash is reproducible and a timeout
        is load-dependent, so folding them together points every investigation at the wrong one,
        and it inflates `parse_timeout_count` with events that were never slow. Measured
        2026-07-30: `tree_sitter_language_pack.process()` **segfaults** on a 10,000-deep
        `f(f(f(…)))` javascript expression (5,000 is fine, and the raw `parse()` + the symbol walk
        both survive 10,000 — it is `process()` alone). HR39 contained it exactly as designed, the
        pool respawned and the next file extracted normally, and the whole event was recorded as a
        parse timeout.

        The re-`get` after `is_alive()` goes false is not belt-and-braces: `Queue.put` hands off to
        a feeder thread, so a worker can legitimately produce a result and then die before the
        parent drains it. Without that second read a successful parse would be reported as a crash.
        """
        deadline = time.monotonic() + deadline_s
        while True:
            try:
                return slot.result_q.get(timeout=min(_POLL_S, max(0.0, deadline - time.monotonic())))
            except queue.Empty:
                pass
            if not slot.proc.is_alive():
                with contextlib.suppress(queue.Empty):
                    return slot.result_q.get(timeout=0.5)  # in-flight result from a since-dead worker
                self._on_worker_gone(idx, path_for_log, PARSE_CRASHED, slot.proc.exitcode)
                return PARSE_CRASHED
            if time.monotonic() >= deadline:
                self._on_worker_gone(idx, path_for_log, PARSE_TIMEOUT, None)
                return PARSE_TIMEOUT

    def _on_worker_gone(self, idx: int, path_for_log: str, why: str, exitcode) -> None:
        with self._lock:
            if why == PARSE_TIMEOUT:
                self.parse_timeout_count += 1
            else:
                self.parse_crash_count += 1
            slot = self._slots[idx]
            slot.proc.terminate()
            slot.proc.join(timeout=5)
            if slot.proc.is_alive():
                slot.proc.kill(); slot.proc.join()
            self._spawn_slot(idx)
        log.warning("bounded_parse %s path_hash=%s exitcode=%s",
                    why, _path_hash(path_for_log), exitcode)

    def idle_shutdown(self, idle_s: float = _IDLE_SHUTDOWN_S) -> None:
        """Free workers after inactivity (HR40); no-op if a task is in flight."""
        if self._slots[0] is None or time.monotonic() - self._last_used < idle_s:
            return
        with self._lock:
            if self._free.qsize() != self._size:
                return
            for i, s in enumerate(self._slots):
                if s is None:
                    continue
                with contextlib.suppress(Exception):
                    s.task_q.put(None)
                s.proc.join(timeout=5)
                if s.proc.is_alive():
                    s.proc.kill(); s.proc.join()
                self._slots[i] = None
            self._free = queue.Queue()
            log.info("bounded_parse pool idle-shutdown")

_pool: BoundedParsePool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> BoundedParsePool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = BoundedParsePool()
    return _pool


def run_bounded(func, args: tuple, deadline_s: float = _DEADLINE_S, path_for_log: str = ""):
    """Run `func` (a module-level, picklable extraction fn) in the bounded pool. Returns its
    return value, `PARSE_TIMEOUT` past `deadline_s`, `PARSE_CRASHED` if the worker died mid-task
    (a signal, or a failure to bootstrap), or `None` on a worker-side exception."""
    return _get_pool().run(func, args, deadline_s, path_for_log)


# `idle_shutdown_check()` was deleted 2026-07-31. It described itself as a scheduler hook in
# `daemon/server.py`, and `daemon/server.py` never called it — nothing did, in the whole tree. It
# is the second half of a pair that documented a mechanism neither half implemented: the systemd
# drop-in `self-heal.conf` reasoned about flap risk from a `check_idle_shutdown()` that has never
# existed under that name either. `_pool.idle_shutdown()` is still there and still correct; what
# is gone is the un-called wrapper that made it look scheduled.


def metrics() -> dict:
    return {"parse_timeout_count": 0 if _pool is None else _pool.parse_timeout_count,
            "parse_crash_count": 0 if _pool is None else _pool.parse_crash_count}
