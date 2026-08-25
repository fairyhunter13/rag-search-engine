"""One line per pin decision, so the rollout has a count with a denominator.

The flag flips when the answered-empty population reaches zero, and until now
that population lived only in journald: capped at seven days, holding no client
name, and countable only by grep. "Zero out of what" is the question a rollout
turns on, and a log that cannot be grouped by client cannot answer it.

Best-effort by construction. A pin decision is not a write this daemon may fail
on, so every error here is swallowed: the ledger is evidence, not state anything
reads back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import config, ledger

if TYPE_CHECKING:  # pragma: no cover
    from .scope import Verdict

NAME = "pins.jsonl"
# One generation, rotated by rename. At ~150 bytes a line this is ~28k
# decisions, which is weeks of this daemon's real traffic.
MAX_BYTES = 4 * 1024 * 1024


def path():
    """Resolved per call, not at import: `STATE_DIR` is what the autouse test
    fixture moves, and a module constant would have kept writing to the fleet's
    real state directory from every test run."""
    return config.STATE_DIR / NAME


def record(verdict: Verdict, roots: int) -> None:
    ledger.append(
        path(),
        {
            "client": verdict.client,
            "proto": verdict.proto,
            "branch": verdict.branch,
            "roots": roots,
            "peer": verdict.peer,
        },
        MAX_BYTES,
    )
