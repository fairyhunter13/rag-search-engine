"""The liveness ping, on a thread that does no work.

The ping was the first statement of the scheduler loop, and the unit's deadline
was three scheduler ticks. That made *slow* indistinguishable from *dead*: the
jobs run after the ping, so one sweep over a 412-project fleet on a loaded
machine outran the 180 s deadline, systemd killed a working daemon, and the
restart re-entered the same load. The sweep measures 2.1 s at the median and
15.4 s at the worst on a quiet card, so what a deadline can honestly catch is a
job that never returns, not a slow one.

Pinging unconditionally is the opposite failure: a watchdog that cannot fire.
So the thread pings only while the scheduler thread is alive and has finished a
cycle inside `SCHEDULER_STALL_S`, and it says in the journal when it stops.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import threading
import time

from . import config

log = logging.getLogger(__name__)

_scheduler: threading.Thread | None = None
_last = 0.0


def notify(state: str) -> None:
    """sd_notify without the systemd python binding, which is a C extension."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    with contextlib.suppress(OSError), sock:
        sock.connect("\0" + addr[1:] if addr.startswith("@") else addr)
        sock.sendall(state.encode())


def beat() -> None:
    """One completed scheduler cycle."""
    global _last
    _last = time.monotonic()


def stall() -> str:
    """Why the ping is withheld, or empty while the daemon is alive."""
    if _scheduler is None:
        return ""
    if not _scheduler.is_alive():
        return "the scheduler thread is gone"
    if (age := time.monotonic() - _last) > config.SCHEDULER_STALL_S:
        return f"no scheduler cycle in {age:.0f}s"
    return ""


def interval_s() -> float:
    """systemd's own deadline over three, not ours: the installed unit can
    predate the config default it was generated from."""
    usec = int(os.environ.get("WATCHDOG_USEC") or 0)
    return (usec / 1e6 if usec > 0 else config.WATCHDOG_SEC) / 3


def disarm() -> None:
    """Nothing to assert once the daemon is stopping."""
    global _scheduler
    _scheduler = None


def _ping(stop: threading.Event) -> None:
    interval, said = interval_s(), ""
    while True:
        why = stall()
        if not why:
            notify("WATCHDOG=1")
        elif why != said:
            log.error("withholding the watchdog ping: %s", why)
        said = why
        if stop.wait(interval):
            return


def start(stop: threading.Event, scheduler: threading.Thread) -> threading.Thread | None:
    """Arm the gate always, and spawn the pinger only under systemd."""
    global _scheduler
    _scheduler = scheduler
    beat()
    if not os.environ.get("NOTIFY_SOCKET"):
        return None
    thread = threading.Thread(target=_ping, args=(stop,), name="watchdog", daemon=True)
    thread.start()
    return thread
