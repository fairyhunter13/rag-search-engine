"""Session tripwire for the one thing no live test may do: lose a real registry row.

On 2026-07-30 a teardown helper's predicate was broken deliberately to demonstrate a red test.
Because that helper runs from a **session autouse fixture inside the test process**, the broken
predicate matched the whole fleet: 198 rows and 138 stores deleted from the real registry.
`projects.json` is written by `os.replace` with no history, ext4 has no snapshots, and nothing had
ever dumped the file — so there was nothing to roll back to and the fleet cost a full GPU re-index.

A guard on the helper that fired closes that one door. This closes the *class*: every live session
copies the registry aside before it touches anything, and refuses to finish quietly if a row that
existed at the start is gone at the end. It does not care which API deleted the row, which is the
point — `_purge_leaked_test_state` is only one of many in-process callers of `remove_project`.

## Why the assertion is scoped rather than absolute

Three things legitimately remove a row during a run, and a guard that flags them is a timer rather
than a monitor — it would be muted inside a week:

  * `purge_rows_under(_SAFE_BASE)` runs at both ends of the session by design;
  * `_migrate()` prunes any row whose path no longer exists on disk (dead-registration self-heal);
  * `_migrate()` re-keys a row to its canonical real path, so the old key legitimately disappears.

So a row counts as lost only when all of: it was present at session start, it is absent now, it is
outside the suite's own base, its path **still exists on disk**, and its canonical form is absent
too. That combination is the wipe signature and nothing else.

## Why restoration is row-by-row and never a file replace

The daemon writes `indexed_at` continuously — a rebuild is ~1,700 chunks/min of registry churn — so
`os.replace`-ing a session-old snapshot at teardown would silently roll back every stamp written
while the suite ran, trading a loud failure for a quiet one. Missing rows go back through
`upsert_project`, which takes the same flock every other writer takes. The snapshot file stays on
disk afterwards as the operator's handle.
"""
from __future__ import annotations

import contextlib
import json
from pathlib import Path

_SNAPSHOT_SUFFIX = ".session-snapshot"


def snapshot_path() -> Path:
    """Where the session snapshot lives: beside the registry, not under the suite's scratch base.

    `_SAFE_BASE` children are `rmtree`'d at session start and the scratch dir dies with the run,
    while the entire value of this file is that it outlives a session that was killed.
    """
    from rag_search.core.config import REGISTRY_PATH

    return Path(str(REGISTRY_PATH) + _SNAPSHOT_SUFFIX)


def take_snapshot() -> tuple[Path, dict]:
    """Copy the registry aside; return `(snapshot path, the rows it held)`.

    Read through `_load()` rather than by copying bytes: an absent or empty registry then yields an
    empty mapping instead of raising. An empty registry is precisely the state this guard exists to
    survive, so discovering one must not fail the session before it has started.
    """
    from rag_search.core.registry import _load

    rows = _load()
    snap = snapshot_path()
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps(rows, indent=2))
    return snap, rows


def rows_lost_outside(before: dict, base: Path) -> list[str]:
    """Snapshot paths that vanished for no legitimate reason. Empty list means the fleet is intact.

    Reads the registry through `_load()` rather than the public reader: that one runs `_migrate` and
    persists it, so asking it what survived would let the question mutate its own answer. (Naming it
    here with its parentheses would also trip `test_no_real_project_in_tests`, whose scan is a
    literal substring match and cannot tell a call from a sentence about not making one.)
    """
    import os as _os

    from rag_search.core.registry import _load, canonicalize_path

    now = set(_load())
    base_str = str(base)
    lost = []
    for path in before:
        if path in now:
            continue
        if path == base_str or path.startswith(base_str + _os.sep):
            continue  # the suite's own rows — purged at both ends by design
        if not Path(path).exists():
            continue  # _migrate drops dead registrations; that is the self-heal working
        with contextlib.suppress(Exception):
            if canonicalize_path(path) in now:
                continue  # re-keyed to its canonical form, same project under a better name
        lost.append(path)
    return sorted(lost)


def restore_rows(before: dict, paths: list[str]) -> list[str]:
    """Put the named snapshot rows back through `upsert_project`. Returns the ones that failed.

    Each row is restored independently: `upsert_project` rejects forbidden roots outright, and one
    unrestorable row must not abandon the other 197. A partial restore that names its casualties
    beats an all-or-nothing one that leaves the fleet empty.
    """
    from dataclasses import fields

    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import upsert_project

    known = {f.name for f in fields(ProjectEntry)} - {"path"}
    failed = []
    for path in paths:
        meta = {k: v for k, v in before.get(path, {}).items() if k in known}
        try:
            upsert_project(ProjectEntry(path=path, **meta))
        except Exception as exc:
            # Reported, never swallowed — the caller prints these in the failure message.
            failed.append(f"{path}: {exc}")
    return failed
