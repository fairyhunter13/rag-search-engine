"""Generate and install the systemd user service unit."""
from __future__ import annotations

from pathlib import Path

from rag_search.core import config

# The deployed unit name, in one place: `scripts/systemd/<UNIT_NAME>.d/` holds the versioned
# operator drop-ins, and systemd only reads a drop-in dir whose name matches the unit exactly.
# It did not match for a year — the tracked dir said `rag-search.service.d`, so the CPU-budget
# drop-in it carried was inert everywhere except this host, where a hand-made copy under the
# real name was doing the work. test_systemd_dropins_target_deployed_unit ties them together.
UNIT_NAME = "rag-search-mcp-daemon.service"

# The unit systemd activates when the daemon lands in `failed`. It existed on this host for months
# as a `static` unit that nothing could ever start: `unit_text()` emitted no `OnFailure=` and neither
# did any drop-in, and the repo's copy was deleted in 18aca54 for having the wrong name without one
# being put back. With `Restart=always` against `StartLimitBurst=20`, the daemon can exhaust its
# restarts, enter `failed` and sit there — which is precisely what the notifier was written for and
# precisely when it was unreachable. Named here so the unit, the `OnFailure=` and the test that
# proves it fires all derive from one string.
NOTIFY_UNIT = "rag-search-mcp-failure-notify.service"


def unit_text(exec_path: str | None = None) -> str:
    if exec_path is None:
        import shutil
        import sys
        # Prefer the binary adjacent to the current Python interpreter (venv-aware).
        _candidate = Path(sys.executable).parent / "rag-search"
        if _candidate.exists():
            exec_path = str(_candidate)
        else:
            exec_path = shutil.which("rag-search") or "rag-search"
    return (
        "[Unit]\n"
        "Description=rag-search singleton MCP daemon (GPU-enforced)\n"
        "After=network.target\n"
        # Fires when the unit reaches `failed` — i.e. after Restart= has given up — not on each
        # restart, so an ordinary crash-and-recover stays silent and only a daemon that has
        # stopped recovering reaches the desktop.
        f"OnFailure={NOTIFY_UNIT}\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        # Bind explicitly, but to the *configured* address rather than a literal. The two
        # were literals here while every client resolved DAEMON_HOST/DAEMON_PORT from
        # RSE_MCP_DAEMON_HOST/_PORT, so setting the port env var and running `install-systemd`
        # produced a unit serving 8765 that nothing was looking at — a misconfiguration with no
        # error anywhere, on the one contract HR34 states as "a fresh clone needs zero source
        # edits to retarget any of them".
        f"ExecStart={exec_path} daemon serve "
        f"--host {config.DAEMON_HOST} --port {config.DAEMON_PORT}\n"
        "Restart=on-failure\n"
        "RestartSec=3s\n"
        "StartLimitBurst=20\n"
        "Environment=RSE_EMBED_DEVICE=cuda\n"
        "EnvironmentFile=-%h/.config/rag-search/env\n"
        "Nice=5\n"
        "CPUWeight=20\n"
        "IOWeight=20\n"
        "MemoryHigh=3G\n"
        "MemoryMax=6G\n"
        # CPUQuota alone does not imply CPUAccounting (systemd#9647) — both required for the
        # kernel-enforced 1-core ceiling (HR40) to actually cap and be readable via cpu.stat.
        "CPUAccounting=yes\n"
        "CPUQuota=100%\n"
        # One federated query opens one sqlite store per member — 157 on the largest federation
        # here — and WAL costs ~3 fds each (db, -wal, -shm). Against systemd's *default* 1024 that
        # is one query landing within a factor of two of the ceiling, and on 2026-07-29 it went
        # over: accept() itself ran out of fds and /healthz served 500 until a restart. The limit
        # has to be derived from the member count, not left at a default chosen for shells.
        "LimitNOFILE=65536\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def notify_unit_text() -> str:
    """The unit `OnFailure=` activates. Oneshot; notifies only if the daemon is still down.

    It re-checks after a pause rather than trusting the transition that woke it. `OnFailure=` fires
    on the way into `failed`, and a unit can be restarted out of that state moments later — a
    notifier without the re-check would page for outages that were over before the popup rendered,
    and a critical notification that is usually wrong gets dismissed reflexively, which is the same
    as not having one.

    `notify-send` is probed rather than required: on a headless host there is no session bus to
    notify, and a notifier that fails the unit it was called about would turn one problem into two.
    """
    check = (
        "sleep 8; "
        f"state=$(systemctl --user is-active {UNIT_NAME} 2>/dev/null || echo unknown); "
        "case $state in active|activating|reloading) exit 0 ;; esac; "
        'command -v notify-send >/dev/null 2>&1 && notify-send -u critical -a rag-search '
        '"rag-search: daemon stopped" '
        '"Daemon crashed and is not recovering automatically.\\n'
        "Check the real cause in the journal, then recover:\\n"
        f"  journalctl --user -u {UNIT_NAME.removesuffix('.service')} -n 40\\n"
        f"  systemctl --user reset-failed {UNIT_NAME.removesuffix('.service')}\\n"
        f'  systemctl --user start {UNIT_NAME.removesuffix(".service")}" || true'
    )
    return (
        "[Unit]\n"
        "Description=rag-search MCP daemon hard-fail desktop notification\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart=/bin/sh -c '{check}'\n"
    )


def install(dest: Path | None = None) -> Path:
    """Write the unit file and its failure notifier; returns the path of the main unit.

    The notifier ships with the unit that references it. `OnFailure=` naming a unit that does not
    exist is not an error systemd reports at load time — it is discovered when the daemon fails,
    which is the one moment nobody is in a position to notice.
    """
    if dest is None:
        dest = Path.home() / ".config" / "systemd" / "user" / UNIT_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(unit_text())
    (dest.parent / NOTIFY_UNIT).write_text(notify_unit_text())
    return dest
