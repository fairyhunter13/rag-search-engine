"""SQLite-backed persistence layer for background jobs.

Jobs survive daemon restarts: the store writes each state transition and
progress update to disk, and the startup replay function re-submits
resumable jobs (currently: enrich_symbols only) on next boot.

DB location: ~/.local/share/opencode-search/jobs.db
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opencode_search.jobs import Job

log = logging.getLogger(__name__)

_DATA_DIR = Path.home() / ".local" / "share" / "opencode-search"
_JOBS_DB_PATH = Path(
    __import__("os").environ.get(
        "OPENCODE_JOBS_DB",
        str(_DATA_DIR / "jobs.db"),
    )
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    project_path TEXT NOT NULL,
    status TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    progress_json TEXT,
    result_json TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS jobs_active ON jobs(status, project_path, action);
"""

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(_JOBS_DB_PATH), check_same_thread=False)
    _conn.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
    _conn.executescript(_SCHEMA)
    _conn.commit()
    return _conn


def upsert_job(job: Job) -> None:
    """Write or update the job row. Thread-safe."""
    try:
        with _lock:
            conn = _get_conn()
            conn.execute(
                """
                INSERT INTO jobs
                    (id, action, project_path, status, queued_at, started_at,
                     completed_at, progress_json, result_json, error)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    started_at=excluded.started_at,
                    completed_at=excluded.completed_at,
                    progress_json=excluded.progress_json,
                    result_json=excluded.result_json,
                    error=excluded.error
                """,
                (
                    job.id,
                    job.action,
                    job.project_path,
                    job.status,
                    job.queued_at,
                    job.started_at,
                    job.completed_at,
                    json.dumps(job.progress) if job.progress is not None else None,
                    json.dumps(job.result) if job.result is not None else None,
                    job.error,
                ),
            )
            conn.commit()
    except Exception as exc:
        log.debug("jobs_store.upsert_job failed: %s", exc)


def load_nonterminal_jobs() -> list[dict[str, Any]]:
    """Return all jobs that were queued/running when the daemon last stopped."""
    try:
        with _lock:
            conn = _get_conn()
            rows = conn.execute(
                "SELECT id, action, project_path, status, queued_at, started_at, "
                "completed_at, progress_json, result_json, error "
                "FROM jobs WHERE status NOT IN ('ok','error','cancelled')"
            ).fetchall()
        result = []
        for row in rows:
            (jid, action, project_path, status, queued_at, started_at,
             completed_at, progress_json, result_json, error) = row
            result.append({
                "id": jid,
                "action": action,
                "project_path": project_path,
                "status": status,
                "queued_at": queued_at,
                "started_at": started_at,
                "completed_at": completed_at,
                "progress": json.loads(progress_json) if progress_json else None,
                "result": json.loads(result_json) if result_json else None,
                "error": error,
            })
        return result
    except Exception as exc:
        log.debug("jobs_store.load_nonterminal_jobs failed: %s", exc)
        return []


def mark_interrupted(job_id: str) -> None:
    """Mark a job as error with 'interrupted by restart' message."""
    try:
        with _lock:
            conn = _get_conn()
            from opencode_search.jobs import _now_iso
            conn.execute(
                "UPDATE jobs SET status='error', error='interrupted by restart', "
                "completed_at=? WHERE id=?",
                (_now_iso(), job_id),
            )
            conn.commit()
    except Exception as exc:
        log.debug("jobs_store.mark_interrupted failed: %s", exc)
