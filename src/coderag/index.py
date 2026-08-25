"""diff -> chunk -> embed -> write, on one worker thread behind one queue.

There is no synchronous index path and no `wait` parameter. Every producer --
the MCP tool, the watcher, the CLI, the startup reconcile -- puts a job on one
`queue.Queue`, and a single worker drains it. The queue *is* the write
serializer, so there is no separate write lock, and one worker means an index
build never contends with itself for the GPU or the disk.

`_GPU_INFER_LOCK` is taken and released per embed batch inside `embed`, so a
user's query interleaves with a running build at batch granularity instead of
waiting for a whole project. That lock is the innermost one: nothing here holds
the registry flock or an open transaction across an embed call.

The whole of staleness is one content-hash diff. It is correct after a crash,
after a missed inotify event, and after a week of downtime, because it asks
whether the store matches the disk rather than tracking how they diverged.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import discover, progress, projcfg, registry, runledger, store
from .indexwrite import _relang, _wipe, _write_files

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Job:
    project: Path
    paths: list[str] | None = None  # None means the whole project
    reason: str = "manual"
    # Stamped by the producer, not the worker. The wait behind one queue is the
    # half of a slow index that the pass itself cannot see.
    queued_at: float = 0.0


@dataclass
class State:
    """What the `index` tool reports. Written by the worker, read by anyone."""

    current: str | None = None
    done: int = 0
    failed: int = 0

    def snapshot(self, depth: int) -> dict:
        out = {
            "state": "indexing" if self.current or depth else "idle",
            "queue_depth": depth,
            "current": self.current,
            "completed": self.done,
            "failed": self.failed,
        }
        return out | ({"progress": inner} if (inner := progress.snapshot()) else {})


_queue: queue.Queue[Job | None] = queue.Queue()
_state = State()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def submit(project: Path | str, paths: list[str] | None = None, reason: str = "manual") -> None:
    """Enqueue a walk, unless an identical whole-project walk is already waiting.

    The hourly reconcile enqueues all 149 rows and `index` is polled for status,
    so the same full walk piles up behind one worker. Only whole-project jobs
    dedup: a partial names files a queued walk may already cover, but dropping
    it on that guess loses a write, where a redundant full walk only wastes one.

    The queue is the state. A shadow set of pending projects has to be cleared
    by whoever dequeues, and anything that is not the worker -- a test, a drain
    on shutdown -- then leaves it claiming a job that no longer exists, which
    silently drops every later submit for that project.
    """
    project = registry.resolve(project)
    if paths is None:
        with _queue.mutex:
            # A concurrent submit can still slip a duplicate past this; the cost
            # of that is one extra walk, and the lock cannot be held across the
            # `put` below, which takes it again.
            if any(
                job is not None and job.paths is None and job.project == project
                for job in _queue.queue
            ):
                return
    _queue.put(Job(project=project, paths=paths, reason=reason, queued_at=time.time()))


def status() -> dict:
    return _state.snapshot(_queue.qsize())


def start_worker() -> threading.Thread:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_drain, name="indexer", daemon=True)
            _worker.start()
        return _worker


def stop_worker(timeout: float = 5.0) -> None:
    _queue.put(None)
    if _worker is not None:
        _worker.join(timeout)


def _drain() -> None:
    while True:
        job = _queue.get()
        if job is None:
            _queue.task_done()
            return
        row = {
            "trace": runledger.trace_id(),
            "project": str(job.project),
            "reason": job.reason,
            "paths": len(job.paths) if job.paths is not None else None,
            "queued_ms": round((time.time() - job.queued_at) * 1000, 2) if job.queued_at else None,
        }
        started = time.perf_counter()
        try:
            _state.current = str(job.project)
            row |= index_project(job.project, job.paths).pop("stage", {})
            _state.done += 1
        except Exception as exc:  # one bad project must not stop the queue
            _state.failed += 1
            row["error"] = f"{type(exc).__name__}: {exc}"
            log.exception("indexing %s failed", job.project)
            with contextlib.suppress(Exception):
                registry.record_error(job.project, f"{type(exc).__name__}: {exc}")
        finally:
            _state.current = None
            # The registry keeps one `last_error` per project and the next
            # failure overwrites it, so this is the only durable copy.
            row["took_ms"] = round((time.perf_counter() - started) * 1000, 2)
            runledger.record("index", row)
            log.debug("index %s %s", job.project, row)
            _queue.task_done()


def index_project(project: Path | str, paths: list[str] | None = None) -> dict:
    """One idempotent pass. Safe to call twice; the second is a no-op.

    Runs on the worker thread. Callers use `submit`.
    """
    project = registry.resolve(project)
    if not project.is_dir():
        # A project that is unmounted, renamed or on a detached drive must not
        # be mistaken for an empty one: the content-hash diff would find no
        # files, delete every chunk it has, and report a successful pass.
        raise FileNotFoundError(f"{project} is not a directory")

    stage: dict = {}
    mark = time.perf_counter()

    def since() -> float:
        nonlocal mark
        now = time.perf_counter()
        elapsed, mark = round((now - mark) * 1000, 2), now
        return elapsed

    entry = registry.get(project)
    roots = tuple(entry.roots) if entry else ()
    cfg = projcfg.effective(project, roots)
    conn = store.connect(project)

    stage["open_ms"] = since()

    if reason := store.incompatible(conn):
        # A store built by another model or another chunker cannot be updated in
        # place: its vectors are in a different space and its line ranges were
        # cut to a different budget.
        log.warning("rebuilding %s: %s", project, reason)
        stage["rebuilt"] = reason
        _wipe(conn)
        conn.commit()

    signature = cfg.signature()
    if store.get_meta(conn, "config_signature") != signature:
        # The excludes changed -- a root was joined or left. This pass has to
        # *remove* what the new config excludes, not merely stop adding to it,
        # so the whole project is walked rather than the requested subset.
        stage["widened"] = True
        paths = None

    stage["relang"] = _relang(conn)

    known = store.file_digests(conn)
    if paths is None:
        write, delete = discover.changed(project, cfg, known)
    else:
        write = [m for m in (discover.read(project, p) for p in paths) if m is not None]
        fresh = {m.rel for m in write}
        delete = [p for p in paths if p not in fresh and p in known]
    stage |= {"known": len(known), "walk_ms": since()}

    deleted = store.delete_files(conn, delete)
    conn.commit()
    stage["delete_ms"] = since()

    written = _write_files(conn, write, project)
    stage["write_ms"] = since()

    store.stamp(conn)
    store.set_meta(conn, config_signature=signature, indexed_at=time.time())
    conn.commit()

    files, chunks = store.counts(conn)
    registry.update(
        project,
        indexed_at=time.time(),
        file_count=files,
        chunk_count=chunks,
        config_signature=signature,
        last_error=None,
    )
    return {
        "project": str(project),
        "files": files,
        "chunks": chunks,
        "written": written,
        "deleted": deleted,
        # `_drain` pops this into its ledger row, and the CLI prints it. The MCP
        # tool never sees it: a model asking for status cannot act on a timing.
        "stage": stage | {"written": written, "deleted": deleted, "files": files, "chunks": chunks},
    }


def suppressed_by_excludes(project: Path | str, roots: tuple[str, ...] = ()) -> int:
    """How many candidate files the inherited excludes are dropping.

    Reported rather than inferred: a user who indexed a repo directly and then
    watched half of it disappear when a root claimed it should be able to see
    why in one call.
    """
    project = registry.resolve(project)
    wide = len(discover.candidates(project, projcfg.load(project)))
    narrow = len(discover.candidates(project, projcfg.effective(project, roots)))
    return max(0, wide - narrow)


def reconcile_all() -> int:
    """Enqueue every enabled project: the startup pass and the hourly sweep.

    This catches everything that changed while the daemon was down, and what a
    project missed while it was dropped from the watch set for a config it
    could not parse. No leases and no pause flag.

    It said "the 60 s tick" for as long as the tick did not call it.
    """
    projects = registry.enabled_projects()
    for entry in projects:
        submit(entry.path, reason="reconcile")
    return len(projects)
