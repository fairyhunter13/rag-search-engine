"""One row per index pass, per watch batch and per sweep: the daemon's own work.

Measured over 24 h before this existed. The registry recorded **454 index passes**
and the journal held **zero lines describing any of them**, while 3,800 of its
5,912 lines were `watchfiles` announcing a change count with no project name. The
work was invisible and the noise was not.

Three kinds share one file, because the question that needs them is one question.
"My edit is not searchable" is answered by a watch row saying the event was
dropped, or by an index row saying the pass ran and failed, and a reader holding
only one of the two cannot tell which happened.

What each kind makes durable, where nothing did:

- `index` -- `_drain` threw away everything `index_project` returned. A pass left
  `indexed_at` and two counts in the registry, overwritten by the next pass.
- `watch` -- `_dispatch` drops an event in three places, each a bare `continue`.
- `sweep` -- a sweep that claims nothing is silent, and it nearly always claims
  nothing. `_tick_errors` and `watch._error` live in memory, so the restart that
  a scheduler failure causes is also what erases it.

Best-effort by construction, like every ledger here: the daemon must not fail on
its own bookkeeping.
"""

from __future__ import annotations

from . import config, ledger

NAME = "runs.jsonl"
# Larger than the other two. A busy hour is hundreds of watch batches, and the
# rows worth having are the ones from before somebody noticed the problem.
MAX_BYTES = 8 * 1024 * 1024

trace_id = ledger.trace_id


def path():
    """Resolved per call, not at import: `STATE_DIR` is what the test fixture
    moves, and a module constant keeps writing to the fleet's real state."""
    return config.STATE_DIR / NAME


def record(kind: str, row: dict) -> None:
    ledger.append(path(), {"kind": kind, **row}, MAX_BYTES)


def read(limit: int = 50, errors_only: bool = False, kind: str = "") -> list[dict]:
    rows = ledger.read(path(), limit if not kind else 4000, errors_only)
    if kind:
        rows = [r for r in rows if r.get("kind") == kind][:limit]
    return rows
