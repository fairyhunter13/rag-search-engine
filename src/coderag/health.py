"""The check that pages when the daemon is up and the fleet is not.

`systemctl is-active` answers "is the process running", and that stayed green
through every project failing to index and through the `Task group is not
initialized` outage, where `/mcp` returned 500 to every request while `/healthz`
was fine. So this asks the daemon, not systemd, and it asks about the fleet.

Persistence is the whole design. `last_error` is cleared by the next success and
the sweep is hourly, so any transient failure holds `projects_failing` at 1 for
up to an hour: a checker that paged on one sample would page for nearly every
one of them, and an alert that cries wolf is how the outage that mattered goes
unread. A project pages only if it was failing at the previous check too.

Two discriminators were tried first and neither works. `last_error_at` is
restamped by every failure, so a project failing at each sweep looks permanently
fresh. `error_total` is a lifetime counter no success resets, so it describes
history rather than now. Only the previous run's failing set answers it, which
is why this holds state.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from . import config


def _previous(state_path: Path) -> set[str]:
    try:
        return set(json.loads(state_path.read_text())["failing"])
    except (OSError, ValueError, KeyError, TypeError):
        # A first run, or state we cannot read. Empty means nothing pages this
        # time, which is right: an unreadable history is not evidence of a
        # failure, and the next check has a real previous set to compare.
        return set()


def check(url: str = "", state_path: Path | None = None, timeout: float = 10.0) -> tuple[bool, str]:
    url = url or config.HEALTHZ_URL
    state_path = state_path or config.HEALTH_STATE_PATH
    try:
        with urllib.request.urlopen(url, timeout=timeout) as reply:
            body = json.loads(reply.read())
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return False, f"{url} did not answer: {exc}"

    # Scheduler jobs join the same set: a dead sweep is "up but not indexing"
    # exactly as a failing project is, and it gets the same persistence rule.
    failing = sorted(
        [str(p) for p in body.get("failing") or []]
        + [f"scheduler:{name}" for name in body.get("scheduler_errors") or {}]
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    stuck = sorted(_previous(state_path) & set(failing))
    state_path.write_text(json.dumps({"failing": failing}, indent=1))

    if stuck:
        return False, f"{len(stuck)} of {body.get('projects', '?')} failing since the last check: " + ", ".join(stuck)
    if failing:
        return True, f"{len(failing)} failing for the first time, watching: " + ", ".join(failing)
    return True, f"{body.get('projects', '?')} projects, none failing"
