"""What one project, together with its federated members, currently holds.

Split out of `tools.py` when that module crossed the executable-line ceiling.
The seam is a real one: every function here reads the registry, the queue and
the watcher, and none of them writes. That is the property `index(status=True)`
sells, and a reader can check it by opening this file alone.

`enroll` calls `of` at the end of its write path, so the same payload answers an
enrolment and a status read. Two shapes for one question is how a caller learns
to parse both.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import index, quiet, registry, watch


def pending(unit: set[Path]) -> int:
    """Walks queued or held for this unit, where `queue_depth` is the fleet's.

    Here rather than in `index`, which owns the queue and does not read it. The
    held half counts: a watch job waits out the quiet window off the queue, and
    reporting 0 there is "I saved a file and nothing happened".
    """
    with index._queue.mutex:
        queued = {j.project for j in index._queue.queue if j is not None and j.project in unit}
    return len(queued | (quiet.projects() & unit))


def of(target: Path, members: list[Path]) -> dict[str, Any]:
    entry = registry.get(target)
    roots = list(entry.roots) if entry else []
    # The unit is the root together with its members, so the counts are too. The
    # root's own row is 33,053 chunks of the 185,453 this project answers from,
    # and reporting it alone told a caller its project was 17.8% built.
    rows = registry.load()
    unit = [row for p in (target, *members) if (row := rows.get(str(p))) and row.enabled]
    out = {
        "root": str(target),
        "members": len(members),
        "roots": roots,
        "indexed": {
            "files": sum(row.file_count for row in unit),
            "chunks": sum(row.chunk_count for row in unit),
            "projects": len(unit),
        },
        # Per project, because that is the grain the store, the watcher and the
        # queue all work at, and one stuck member is invisible in any total.
        "root_indexed": {
            "files": entry.file_count if entry else 0,
            "chunks": entry.chunk_count if entry else 0,
        },
        "pending": pending({row.path for row in unit}),
        "members_watching": sum(1 for row in unit if row.path != target and watch.armed(row.path)),
        "member_errors": [
            {"project": str(row.path), "error": row.last_error}
            for row in unit
            if row.path != target and row.last_error
        ],
        "suppressed_by_inherited_excludes": index.suppressed_by_excludes(target, tuple(roots)),
        "last_error": entry.last_error if entry else None,
        # Durable: last_error is cleared by the next success, so on an hourly reconcile
        # these are the only trace a failure that resolved itself ever leaves.
        "last_error_at": entry.last_error_at if entry else None,
        "error_total": entry.error_total if entry else 0,
        "watching": watch.armed(target),
    }
    return out | index.status()
