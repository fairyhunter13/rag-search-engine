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
from dataclasses import dataclass, field
from pathlib import Path

from . import chunk as chunker
from . import discover, embed, projcfg, registry, store

log = logging.getLogger(__name__)

# Files per transaction. One commit per file fsyncs 61,714 times; one commit per
# project leaves a multi-hour build with nothing durable if the daemon stops.
BATCH_FILES = 64


@dataclass(slots=True)
class Job:
    project: Path
    paths: list[str] | None = None  # None means the whole project
    reason: str = "manual"


@dataclass
class State:
    """What the `index` tool reports. Written by the worker, read by anyone."""

    current: str | None = None
    done: int = 0
    failed: int = 0
    started_at: float = field(default_factory=time.time)

    def snapshot(self, depth: int) -> dict:
        return {
            "state": "indexing" if self.current or depth else "idle",
            "queue_depth": depth,
            "current": self.current,
            "completed": self.done,
            "failed": self.failed,
        }


_queue: queue.Queue[Job | None] = queue.Queue()
_state = State()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def submit(project: Path | str, paths: list[str] | None = None, reason: str = "manual") -> None:
    _queue.put(Job(project=registry.resolve(project), paths=paths, reason=reason))


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
        try:
            _state.current = str(job.project)
            index_project(job.project, job.paths)
            _state.done += 1
        except Exception as exc:  # one bad project must not stop the queue
            _state.failed += 1
            log.exception("indexing %s failed", job.project)
            with contextlib.suppress(Exception):
                registry.update(job.project, last_error=f"{type(exc).__name__}: {exc}")
        finally:
            _state.current = None
            _queue.task_done()


def _wipe(conn) -> None:
    for table in ("chunks_fts", "chunks_vec", "chunks", "files"):
        conn.execute(f"DELETE FROM {table}")


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

    entry = registry.get(project)
    roots = tuple(entry.roots) if entry else ()
    cfg = projcfg.effective(project, roots)
    conn = store.connect(project)

    if reason := store.incompatible(conn):
        # A store built by another model or another chunker cannot be updated in
        # place: its vectors are in a different space and its line ranges were
        # cut to a different budget.
        log.warning("rebuilding %s: %s", project, reason)
        _wipe(conn)
        conn.commit()

    signature = cfg.signature()
    if store.get_meta(conn, "config_signature") != signature:
        # The excludes changed -- a root was joined or left. This pass has to
        # *remove* what the new config excludes, not merely stop adding to it,
        # so the whole project is walked rather than the requested subset.
        paths = None

    known = store.file_digests(conn)
    if paths is None:
        write, delete = discover.changed(project, cfg, known)
    else:
        write = [m for m in (discover.read(project, p) for p in paths) if m is not None]
        fresh = {m.rel for m in write}
        delete = [p for p in paths if p not in fresh and p in known]

    deleted = store.delete_files(conn, delete)
    conn.commit()

    written = _write_files(conn, write)

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
    }


def _write_files(conn, metas: list) -> int:
    """Chunk, embed and store, committing every BATCH_FILES.

    The embed call sits outside the transaction on purpose: it is the slow part
    and it takes the GPU lock, and holding a SQLite write transaction across it
    would block every reader for the length of a batch.
    """
    written, pending = 0, []
    for meta in metas:
        chunks = chunker.chunk_text(meta.text, rel_path=meta.rel)
        if not chunks:
            continue
        vectors = embed.get_embedder().embed([c.embed_text for c in chunks], side=embed.DOCUMENT)
        pending.append((meta, chunks, vectors))
        if len(pending) >= BATCH_FILES:
            written += _flush(conn, pending)
            pending = []
    return written + _flush(conn, pending)


def _flush(conn, pending: list) -> int:
    for meta, chunks, vectors in pending:
        store.upsert_file(
            conn,
            {
                "path": meta.rel,
                "mtime": meta.mtime,
                "size": meta.size,
                "sha256": meta.sha256,
                "lang": meta.lang,
                "n_lines": meta.n_lines,
            },
            chunks,
            vectors,
        )
    if pending:
        conn.commit()
    return len(pending)


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
    """Enqueue every enabled project: the startup pass and the 60 s tick.

    This catches everything that changed while the daemon was down, and it is
    the only reconcile -- no sweeps, no leases, no pause flag.
    """
    projects = registry.enabled_projects()
    for entry in projects:
        submit(entry.path, reason="reconcile")
    return len(projects)
