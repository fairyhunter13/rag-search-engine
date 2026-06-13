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
        tick = min(1.0, min(j.interval_s for j in self._jobs) / 2) if self._jobs else 1.0
        self._thread = threading.Thread(
            target=self._loop, args=(tick,), daemon=True, name="ocs-scheduler"
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _loop(self, tick: float) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            for job in self._jobs:
                if now - job._last_run >= job.interval_s:
                    job._last_run = now
                    try:
                        job.fn()
                    except Exception as exc:
                        log.warning("job %s failed: %s", job.name, exc)
            self._stop.wait(timeout=tick)
