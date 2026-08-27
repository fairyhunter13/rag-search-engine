"""Hold a watch job until the project stops changing.

A watch batch carries one event. An editor saving through a build produced 303
index passes in 15 minutes across this fleet, each one a full content-hash walk
of the project, so the cost is the walk and never the one file that moved.
Debouncing in the watcher cannot merge them: the events arrive seconds apart,
which is far outside any batch window that still feels immediate.

A held job that never runs is not a correctness problem. Staleness is one
content-hash diff, so the hourly reconcile catches whatever a shutdown drops.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path


class _Held:
    __slots__ = ("paths", "ready_at", "reason")

    def __init__(self, paths: list[str] | None, reason: str, ready_at: float) -> None:
        self.paths: set[str] | None = None if paths is None else set(paths)
        self.reason = reason
        self.ready_at = ready_at


_lock = threading.Lock()
_held: dict[Path, _Held] = {}


def hold(project: Path, paths: list[str] | None, reason: str, delay: float) -> None:
    """Queue this project for `delay` from now, restarting any countdown on it.

    A second event widens the job and pushes the deadline out. `paths=None` is
    the whole project and absorbs any partial already held for it.
    """
    ready_at = time.monotonic() + delay
    with _lock:
        live = _held.get(project)
        if live is None:
            _held[project] = _Held(paths, reason, ready_at)
            return
        live.ready_at = ready_at
        live.reason = reason
        if paths is None or live.paths is None:
            live.paths = None
        else:
            live.paths |= set(paths)


def due() -> list[tuple[Path, list[str] | None, str]]:
    """Every project whose countdown has expired, removed from the hold."""
    now = time.monotonic()
    with _lock:
        ready = [p for p, h in _held.items() if h.ready_at <= now]
        return [_take(p) for p in ready]


def release(project: Path) -> tuple[Path, list[str] | None, str] | None:
    """Take a held job now, for an explicit call that must not wait behind it."""
    with _lock:
        return _take(project) if project in _held else None


def pending() -> int:
    with _lock:
        return len(_held)


def projects() -> set[Path]:
    with _lock:
        return set(_held)


def flush() -> list[tuple[Path, list[str] | None, str]]:
    """Take everything held, countdown or not."""
    with _lock:
        return [_take(p) for p in list(_held)]


def _take(project: Path) -> tuple[Path, list[str] | None, str]:
    """Caller holds `_lock`."""
    live = _held.pop(project)
    return project, (None if live.paths is None else sorted(live.paths)), live.reason
