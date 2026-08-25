"""Append a JSONL row, rotate one generation by rename, read both back.

Three ledgers hold the same twenty lines: pins, searches and index runs. This is
that block, taking the path and the cap as arguments so each ledger keeps its own
`NAME` and its own `path()` -- which is what the test fixtures move.

Best-effort by construction. A ledger is evidence, and nothing reads it back as
state, so a daemon must not fail on a write to one.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import time
from pathlib import Path


def trace_id() -> str:
    """Short enough for a caller to quote back out of an error message."""
    return secrets.token_hex(4)


def append(target: Path, row: dict, max_bytes: int) -> None:
    with contextlib.suppress(OSError):
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size >= max_bytes:
            target.replace(target.with_suffix(".jsonl.1"))
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": round(time.time(), 3), **row}) + "\n")


def read(target: Path, limit: int = 50, errors_only: bool = False) -> list[dict]:
    """The newest rows first, across both generations.

    The rotated generation is read too. A rotation the moment before a question
    is asked would otherwise answer it with an empty file.
    """
    rows: list[dict] = []
    for name in (target, target.with_suffix(".jsonl.1")):
        with contextlib.suppress(OSError):
            for line in name.read_text(encoding="utf-8").splitlines():
                with contextlib.suppress(ValueError):
                    rows.append(json.loads(line))
    rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
    if errors_only:
        rows = [r for r in rows if r.get("error")]
    return rows[:limit]
