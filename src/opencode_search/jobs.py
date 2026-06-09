"""Background job registry for long-running build actions.

Provides fire-and-forget task dispatch with status tracking.
Jobs are in-process only — not persisted across daemon restarts.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Job data model
# ---------------------------------------------------------------------------

_TERMINAL = {"ok", "error", "cancelled"}
_MAX_JOBS = 200  # oldest completed jobs are evicted beyond this cap


@dataclass
class Job:
    id: str
    action: str
    project_path: str
    status: str  # "queued" | "running" | "ok" | "error" | "cancelled"
    queued_at: str
    started_at: str | None = None
    completed_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    progress: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_jobs: dict[str, Job] = {}
_bg_tasks: set[asyncio.Task] = set()
_job_tasks: dict[str, asyncio.Task] = {}  # job_id → task, for cancellation
_dedup_lock = threading.Lock()  # serialises the check-then-create in submit_job(dedup=True)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _evict_old_jobs() -> None:
    """Keep registry under _MAX_JOBS by dropping oldest completed jobs."""
    completed = [j for j in _jobs.values() if j.status in _terminal]
    if len(_jobs) > _MAX_JOBS and completed:
        completed.sort(key=lambda j: j.completed_at or "")
        for j in completed[: len(_jobs) - _MAX_JOBS]:
            _jobs.pop(j.id, None)

_terminal = _TERMINAL  # module-level alias used inside functions


def find_active_job(*, project_path: str, action: str) -> Job | None:
    """Return the in-flight (queued/running) job for this (project, action) pair.

    Used by `submit_job(dedup=True)` to avoid spawning concurrent duplicate
    builds that would race on the same LanceDB tables and graph DB.
    """
    for j in _jobs.values():
        if j.status in _TERMINAL:
            continue
        if j.project_path == project_path and j.action == action:
            return j
    return None


def submit_job(
    coro: Coroutine[Any, Any, dict[str, Any]],
    *,
    action: str,
    project_path: str,
    dedup: bool = False,
) -> Job:
    """Schedule *coro* as a background asyncio task and return the Job immediately.

    The coroutine must return a dict; its ``status`` key drives the job outcome.

    When *dedup* is True, an existing in-flight Job for (project_path, action)
    is returned instead of spawning a new one — the passed-in coroutine is
    closed to silence the "coroutine was never awaited" warning.
    """
    with _dedup_lock:
        if dedup:
            existing = find_active_job(project_path=project_path, action=action)
            if existing is not None:
                with contextlib.suppress(Exception):
                    coro.close()
                log.info(
                    "submit_job: dedup hit — returning existing job[%s] for %s(%s)",
                    existing.id, action, project_path,
                )
                return existing
        job_id = str(uuid.uuid4())[:8]
        job = Job(
            id=job_id,
            action=action,
            project_path=project_path,
            status="queued",
            queued_at=_now_iso(),
        )
        _jobs[job_id] = job
    _evict_old_jobs()

    # Persist the queued state to SQLite for restart recovery.
    import contextlib as _ctx
    with _ctx.suppress(Exception):
        from opencode_search.jobs_store import upsert_job as _upsert
        _upsert(job)

    async def _run() -> None:
        job.status = "running"
        job.started_at = _now_iso()
        log.info("job[%s] %s(%s) started", job_id, action, project_path)
        with _ctx.suppress(Exception):
            from opencode_search.jobs_store import upsert_job as _upsert
            _upsert(job)
        try:
            result = await coro
            job.result = result
            job.status = result.get("status", "ok") if isinstance(result, dict) else "ok"
            if job.status not in _TERMINAL:
                job.status = "ok"
            log.info("job[%s] %s finished: %s", job_id, action, job.status)
        except asyncio.CancelledError:
            job.status = "cancelled"
            log.info("job[%s] %s cancelled", job_id, action)
        except Exception as exc:
            job.status = "error"
            job.error = str(exc)
            log.warning("job[%s] %s error: %s", job_id, action, exc)
        finally:
            job.completed_at = _now_iso()
            with _ctx.suppress(Exception):
                from opencode_search.jobs_store import upsert_job as _upsert
                _upsert(job)

    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(_run(), name=f"job:{job_id}:{action}")
        _bg_tasks.add(task)
        _job_tasks[job_id] = task
        task.add_done_callback(_bg_tasks.discard)
        task.add_done_callback(lambda _: _job_tasks.pop(job_id, None))
    except RuntimeError:
        job.status = "error"
        job.error = "no running event loop"
        job.completed_at = _now_iso()
        log.warning("job[%s]: no running event loop — cannot schedule", job_id)
        with _ctx.suppress(Exception):
            from opencode_search.jobs_store import upsert_job as _upsert
            _upsert(job)

    return job


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def list_jobs(*, project_path: str | None = None, action: str | None = None) -> list[Job]:
    jobs = list(_jobs.values())
    if project_path:
        jobs = [j for j in jobs if j.project_path == project_path]
    if action:
        jobs = [j for j in jobs if j.action == action]
    return sorted(jobs, key=lambda j: j.queued_at, reverse=True)


def cancel_job(job_id: str) -> bool:
    """Cancel a running job. Returns True if found and cancel was requested."""
    job = _jobs.get(job_id)
    if job is None or job.status in _TERMINAL:
        return False
    task = _job_tasks.get(job_id)
    if task and not task.done():
        task.cancel()
        return True
    return False


def job_to_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "action": job.action,
        "project_path": job.project_path,
        "status": job.status,
        "queued_at": job.queued_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "error": job.error,
        "result": job.result,
        "progress": job.progress,
    }
