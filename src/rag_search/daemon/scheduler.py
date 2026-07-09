"""Single-instance background scheduler with registered jobs."""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class _Job:
    name: str
    fn: Callable[[], None]
    interval_s: float
    _last_run: float = field(default=0.0, repr=False)


class Scheduler:
    """Background thread running registered jobs at their configured intervals."""

    def __init__(self) -> None:
        self._jobs: list[_Job] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def register(self, name: str, fn: Callable[[], None], interval_s: float) -> None:
        self._jobs.append(_Job(name=name, fn=fn, interval_s=interval_s))

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="rse-scheduler"
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            next_deadline = now + 3600.0  # fallback: wake at most once per hour when idle
            for job in self._jobs:
                due_at = job._last_run + job.interval_s
                if now >= due_at:
                    job._last_run = now
                    try:
                        job.fn()
                    except Exception as exc:
                        log.warning("job %s failed: %s", job.name, exc)
                    due_at = job._last_run + job.interval_s
                next_deadline = min(next_deadline, due_at)
            wait_s = max(0.0, next_deadline - time.monotonic())
            self._stop.wait(timeout=wait_s)
