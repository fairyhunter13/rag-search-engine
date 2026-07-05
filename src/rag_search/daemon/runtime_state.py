"""Runtime activity tracking: idle shutdown."""
from __future__ import annotations

import sys
import threading
import time

from rag_search.core.config import IDLE_SHUTDOWN_S

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


def check_idle_shutdown() -> None:
    """Exit if idle longer than IDLE_SHUTDOWN_S (called by scheduler)."""
    if seconds_since_activity() > IDLE_SHUTDOWN_S:
        sys.exit(0)
