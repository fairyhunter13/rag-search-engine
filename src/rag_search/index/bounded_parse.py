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
_DEADLINE_S = float(os.environ.get("OPENCODE_BOUNDED_PARSE_DEADLINE_S", "10"))
_POOL_SIZE = int(os.environ.get("OPENCODE_BOUNDED_PARSE_WORKERS", "1"))  # HR40: 1-core quota
_IDLE_SHUTDOWN_S = float(os.environ.get("OPENCODE_BOUNDED_PARSE_IDLE_S", "120"))
_CTX = mp.get_context("spawn")


def _path_hash(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()[:12]  # non-reversible log id, never the real path (HR34)


def _worker_main(task_q, result_q) -> None:
    """Persistent worker loop. CPU-only — must never import the embedder/CUDA."""
    while True:
        item = task_q.get()
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
        proc = _CTX.Process(target=_worker_main, args=(task_q, result_q), daemon=True)
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
            slot.task_q.put((func, args))
            try:
                status, payload = slot.result_q.get(timeout=deadline_s)
            except queue.Empty:
                self._on_timeout(idx, path_for_log)
                return PARSE_TIMEOUT
            if status == "error":
                log.warning("bounded_parse worker error path_hash=%s: %s",
                            _path_hash(path_for_log), payload)
                return None
            return payload
        finally:
            self._free.put(idx)

    def _on_timeout(self, idx: int, path_for_log: str) -> None:
        with self._lock:
            self.parse_timeout_count += 1
            slot = self._slots[idx]
            slot.proc.terminate()
            slot.proc.join(timeout=5)
            if slot.proc.is_alive():
                slot.proc.kill(); slot.proc.join()
            self._spawn_slot(idx)
        log.warning("bounded_parse PARSE_TIMEOUT path_hash=%s", _path_hash(path_for_log))

    def idle_shutdown(self, idle_s: float = _IDLE_SHUTDOWN_S) -> None:
        """Free workers after inactivity (P16/P17); no-op if a task is in flight."""
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
    return value, `PARSE_TIMEOUT` past `deadline_s`, or `None` on a worker-side exception."""
    return _get_pool().run(func, args, deadline_s, path_for_log)


def idle_shutdown_check() -> None:
    """Scheduler hook (daemon/server.py) — frees pool workers after sustained inactivity."""
    if _pool is not None:
        _pool.idle_shutdown()

def metrics() -> dict:
    return {"parse_timeout_count": 0 if _pool is None else _pool.parse_timeout_count}
