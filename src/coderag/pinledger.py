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

import contextlib
import json
import time
from typing import TYPE_CHECKING

from . import config

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
    row = {
        "ts": round(time.time(), 3),
        "client": verdict.client,
        "proto": verdict.proto,
        "branch": verdict.branch,
        "roots": roots,
        "peer": verdict.peer,
    }
    target = path()
    with contextlib.suppress(OSError):
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size >= MAX_BYTES:
            target.replace(target.with_suffix(".jsonl.1"))
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
