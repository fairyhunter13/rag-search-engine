"""The user unit, and the alert that waits before it pages.

`LimitNOFILE=65536` is not a round number chosen for comfort: 150 repos at three
fds each under WAL is the arithmetic that wedged the previous daemon with a file
descriptor leak against the default 1024.

`Restart=on-failure` is safe only because `server._shutdown_exit` calls
`os._exit` -- the CUDA EP aborts with 134 during CPython finalization, and
without that exit this line is a restart loop.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import config

UNIT_NAME = f"{config.APP}.service"
ALERT_NAME = f"{config.APP}-alert@.service"
HEALTH_NAME = f"{config.APP}-health.service"
HEALTH_TIMER = f"{config.APP}-health.timer"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"


def unit_text(executable: str = "") -> str:
    return f"""\
[Unit]
Description=coderag MCP daemon
After=graphical-session.target
# [Unit], not [Service]: systemd logs "Unknown key name" and carries on, so a
# misplaced one is an alert that never fires and never says it did not.
OnFailure={config.APP}-alert@%n.service

[Service]
Type=notify
NotifyAccess=all
WatchdogSec={config.SCHEDULER_TICK_S * 3}
ExecStart={executable or sys.executable} -m coderag.cli serve
Restart=on-failure
RestartSec=5
LimitNOFILE=65536
# A strictly-later backstop behind server.py's own {config.SHUTDOWN_DEADLINE_S} s deadline.
# Without it systemd's 90 s default applied, which is the exact window the stop
# outage was measured in -- and at 90 s a SIGKILL reads as a failure and pages.
TimeoutStopSec={config.SHUTDOWN_DEADLINE_S + 5}
# This is a background indexer sharing a laptop with the person using it. The
# weights are shares, not caps: an idle machine still gets the whole card and
# the whole disk, and a busy one stops losing its scroll to a fleet pass.
Nice=10
CPUWeight=20
IOWeight=20
# High, not Max: over the limit the kernel reclaims and throttles rather than
# OOM-killing, and a killed daemon loses the walk. Measured 2.14-2.29 GiB with
# both models resident, so 4G is headroom for a batch and not a ceiling to hit.
MemoryHigh=4G
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""


def alert_text() -> str:
    """Wait, re-check, and only then page.

    A daemon that restarts cleanly in five seconds is not an incident, and a
    desktop notification for every one of those is how an alert gets muted --
    after which the outage that mattered is silent too.
    """
    # "coderag down" was wrong once the health unit started using this path: the
    # fleet failing to index is a daemon that is up. %i names which.
    return """\
[Unit]
Description=coderag failure alert for %i

[Service]
Type=oneshot
ExecStartPre=/bin/sleep 8
ExecStart=/bin/sh -c 'systemctl --user is-active --quiet %i || \
  notify-send -u critical "coderag" "%i failed, re-checked after 8s"'
"""


def health_text(executable: str = "") -> str:
    """`is-active` is a liveness check, and liveness stayed green through every
    project failing to index. This asks the daemon about the fleet instead, and
    `OnFailure` reuses the one notification path rather than adding a second."""
    return f"""\
[Unit]
Description=coderag fleet health check
OnFailure={config.APP}-alert@%n.service

[Service]
Type=oneshot
ExecStart={executable or sys.executable} -m coderag.cli health
"""


def health_timer_text() -> str:
    # At least a sweep apart: checked twice inside one, every failure reads as
    # still-failing because nothing has had the chance to retry it yet.
    return f"""\
[Unit]
Description=hourly coderag fleet health check

[Timer]
OnBootSec=15min
OnUnitActiveSec={config.HEALTH_EVERY_S}s

[Install]
WantedBy=timers.target
"""


def install(enable: bool = True) -> Path:
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    unit = UNIT_DIR / UNIT_NAME
    unit.write_text(unit_text())
    (UNIT_DIR / ALERT_NAME).write_text(alert_text())
    (UNIT_DIR / HEALTH_NAME).write_text(health_text())
    (UNIT_DIR / HEALTH_TIMER).write_text(health_timer_text())

    _systemctl("daemon-reload")
    if enable:
        _systemctl("enable", "--now", UNIT_NAME)
        # The timer, not the service: enabling the oneshot would run the check
        # at every boot and never again.
        _systemctl("enable", "--now", HEALTH_TIMER)
    return unit


def _systemctl(*args: str) -> int:
    if not os.environ.get("XDG_RUNTIME_DIR"):
        print("no XDG_RUNTIME_DIR; wrote the unit but did not reload systemd")
        return 1
    return subprocess.run(["systemctl", "--user", *args], check=False).returncode
