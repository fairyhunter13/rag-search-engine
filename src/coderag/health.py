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


def _previous(state_path: Path) -> dict:
    try:
        was = json.loads(state_path.read_text())
        return {"failing": set(was["failing"]), "queue_depth": was.get("queue_depth")}
    except (OSError, ValueError, KeyError, TypeError):
        # A first run, or state we cannot read. Empty means nothing pages this
        # time, which is right: an unreadable history is not evidence of a
        # failure, and the next check has a real previous set to compare.
        return {"failing": set(), "queue_depth": None}


def _silent(body: dict, was: dict) -> tuple[list[str], set[str]]:
    """The failures no project row carries, as identities the same rule can hold.

    Three readings the checker took as healthy. A watcher thread that died hours
    ago, because only `failing` and `scheduler_errors` were read. A queue that
    stopped draining, for the same reason. And a fleet failing past
    `HEALTH_FAILING_CAP`, where `failing` is truncated and `projects_failing`
    carries the true count unread.

    Each identity is a constant string, because the two-sample rule compares
    identities: one that carries a count changes between the samples and pages
    on neither.

    The second return is the identities that already hold two samples of their
    own. Only the queue does: "deep, and no shallower than at the last check" is
    the same comparison the rule makes, and running it twice would page three
    checks after the stall rather than at it.
    """
    silent = []
    # Absent means an older daemon, and an unreadable field is not evidence.
    if body.get("watching", True) is False:
        silent.append("watcher:the thread is not running")

    proven = set()
    depth = (body.get("indexer") or {}).get("queue_depth") or 0
    before = was["queue_depth"]
    if depth >= config.HEALTH_QUEUE_STUCK and before is not None and depth >= before:
        stalled = "indexer:the queue is not draining"
        silent.append(stalled)
        proven.add(stalled)

    listed = len(body.get("failing") or [])
    if int(body.get("projects_failing") or 0) > listed:
        silent.append(f"registry:more than {listed} projects failing, past the reply's cap")
    return silent, proven


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
    was = _previous(state_path)
    silent, proven = _silent(body, was)
    failing = sorted(
        [str(p) for p in body.get("failing") or []]
        + [f"scheduler:{name}" for name in body.get("scheduler_errors") or {}]
        + silent
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    stuck = sorted((was["failing"] | proven) & set(failing))
    depth = (body.get("indexer") or {}).get("queue_depth") or 0
    state_path.write_text(json.dumps({"failing": failing, "queue_depth": depth}, indent=1))

    if stuck:
        return False, f"{len(stuck)} of {body.get('projects', '?')} failing since the last check: " + ", ".join(stuck)
    if failing:
        return True, f"{len(failing)} failing for the first time, watching: " + ", ".join(failing)
    return True, f"{body.get('projects', '?')} projects, none failing"
