"""Runtime activity tracking: idle shutdown + client registry."""
from __future__ import annotations

import sys
import threading
import time

from opencode_search.core.config import CLIENT_STALE_S, IDLE_SHUTDOWN_S

_lock = threading.Lock()
_last_activity: float = time.monotonic()
_clients: dict[str, float] = {}  # client_id -> last_seen monotonic


def note_activity() -> None:
    global _last_activity
    with _lock:
        _last_activity = time.monotonic()


def note_query(query: str) -> None:
    note_activity()


def seconds_since_activity() -> float:
    with _lock:
        return time.monotonic() - _last_activity


def register_client(client_id: str) -> None:
    with _lock:
        _clients[client_id] = time.monotonic()
        _last_activity = time.monotonic()


def heartbeat_client(client_id: str) -> None:
    with _lock:
        _clients[client_id] = time.monotonic()
        _last_activity = time.monotonic()


def release_client(client_id: str) -> None:
    with _lock:
        _clients.pop(client_id, None)


def release_stale_clients() -> list[str]:
    """Remove clients silent > CLIENT_STALE_S; return removed IDs."""
    now = time.monotonic()
    with _lock:
        stale = [cid for cid, ts in _clients.items() if now - ts > CLIENT_STALE_S]
        for cid in stale:
            del _clients[cid]
    return stale


def check_idle_shutdown() -> None:
    """Exit if idle longer than IDLE_SHUTDOWN_S (called by scheduler)."""
    if seconds_since_activity() > IDLE_SHUTDOWN_S:
        sys.exit(0)
