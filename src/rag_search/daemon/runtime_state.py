"""Runtime activity tracking (feeds idle model-unload + healthz idle_seconds).

The daemon is an always-on HTTP MCP server (systemd `Restart=always`, clients cannot
respawn it), so it does NOT self-exit on idle — idle resource savings are handled by
`server._idle_unload` freeing the embedder/reranker while the process stays up. Do not
re-add a process idle-exit here: a prior `sys.exit(0)` ran in the scheduler's background
thread, where `SystemExit` is swallowed and never terminated the process anyway.
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_last_activity: float = time.monotonic()


def note_activity() -> None:
    global _last_activity
    with _lock:
        _last_activity = time.monotonic()


def note_query(query: str) -> None:
    note_activity()


def seconds_since_activity() -> float:
    with _lock:
        return time.monotonic() - _last_activity
