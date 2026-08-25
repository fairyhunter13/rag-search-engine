"""One row per search, holding every stage boundary the reply does not carry.

The reply states `took_ms` and how many projects were scanned. Between those two
numbers the pool is built, filtered, cut, reranked and diversified, and none of
those sizes was written down anywhere. So `pool_cut` starved 307 of the 338
projects that produced a candidate, and finding it needed an offline replay that
rebuilt the pool by hand.

A ledger rather than log lines, and the reason is dated. [A failure that resolved
itself left no trace](../../knowledge/defects/a-failure-that-resolved-itself-left-no-trace.md)
refused a louder journal on a measurement: the volume is already thousands of
lines a day, and what was missing was structure, not prose. A row is groupable
by client, by root and by stage. A log line is greppable and nothing else.

Best-effort by construction, the same as `pinledger`: a search must not fail on
its own bookkeeping.
"""

from __future__ import annotations

from . import config, ledger

NAME = "searches.jsonl"
# One generation, rotated by rename. At ~400 bytes a row this holds ~10k
# searches, and the fleet runs a few hundred a day.
MAX_BYTES = 4 * 1024 * 1024

trace_id = ledger.trace_id


def path():
    """Resolved per call, not at import: `STATE_DIR` is what the test fixture
    moves, and a module constant keeps writing to the fleet's real state."""
    return config.STATE_DIR / NAME


def record(row: dict) -> None:
    ledger.append(path(), row, MAX_BYTES)


def read(limit: int = 50, errors_only: bool = False) -> list[dict]:
    return ledger.read(path(), limit, errors_only)
