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
    # Absolute monotonic deadline, set by register() — never a "last run" of 0.0. That default
    # made *every* job due at process t=0, because `_last_run + interval_s` was compared against
    # time.monotonic(), which is seconds since boot (~415,000 s on the host where this was found).
    # A job written "6 h; CPU/disk only" therefore ran on all ~20 daemon starts a day and almost
    # never on its interval: 35 orphan-vacuum bursts in 3 days, 34 of them inside 0-1 s of a start,
    # exactly one at the real 21,580 s — competing with member discovery and watcher sync on a
    # one-core CPUQuota, with measured 8.6 s VACUUMs at startup+45 s.
    _next_run: float = field(default=0.0, repr=False)


class Scheduler:
    """Background thread running registered jobs at their configured intervals."""

    def __init__(self) -> None:
        self._jobs: list[_Job] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def register(
        self,
        name: str,
        fn: Callable[[], None],
        interval_s: float,
        run_at_start: bool = False,
    ) -> None:
        """Register a job. It first runs one interval from now unless run_at_start is True.

        `run_at_start=True` is for the cheap liveness ticks (a watchdog ping, an idle check)
        where firing immediately is the point; heavy periodic work must not land in the startup
        window, which is the busiest moment the daemon has.
        """
        now = time.monotonic()
        self._jobs.append(_Job(
            name=name, fn=fn, interval_s=interval_s,
            _next_run=now if run_at_start else now + interval_s,
        ))

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
                if now >= job._next_run:
                    # Stamped before the call, as before: the interval runs from start to start,
                    # so a slow job does not push its own next tick further out.
                    job._next_run = now + job.interval_s
                    try:
                        job.fn()
                    except Exception as exc:
                        log.warning("job %s failed: %s", job.name, exc)
                next_deadline = min(next_deadline, job._next_run)
            wait_s = max(0.0, next_deadline - time.monotonic())
            self._stop.wait(timeout=wait_s)
