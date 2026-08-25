"""`coderag trace` -- read a ledger back, one line per row.

Split out of `cli.py` at the 300-line ceiling, and there are two shapes. A
search row has fixed stages worth aligning. A run row is read when something
did not happen, so it prints every field it holds.
"""

from __future__ import annotations

import time

from . import runledger, searchledger


def render(kind: str, n: int, errors: bool) -> int:
    return _search_rows(n, errors) if kind == "search" else _run_rows(kind, n, errors)


def _search_rows(n: int, errors: bool) -> int:
    """The search ledger, newest first. Stage sizes side by side is the point.

    `pool` against `cut` is the pair that names a starved round-robin, and
    `unit` against `pool_projects` names a federation that reached nothing.
    """
    rows = searchledger.read(n, errors)
    if not rows:
        print(f"no rows in {searchledger.path()}")
        return 0
    for row in rows:
        when = time.strftime("%m-%d %H:%M:%S", time.localtime(row.get("ts", 0)))
        head = f"{when} {row.get('trace', '-')} {row.get('client', '-')}"
        if row.get("error"):
            print(f"{head} FAILED {row['error']}")
            continue
        print(
            f"{head} {row.get('root', '-')}\n"
            f"    unit={row.get('unit')} pool={row.get('pool')}"
            f" from {row.get('pool_projects')} proj"
            f" -> filtered={row.get('filtered')} cut={row.get('cut')}"
            f" from {row.get('cut_projects')} proj"
            f" -> returned={row.get('returned')}\n"
            f"    embed={row.get('embed_ms')}ms retrieve={row.get('retrieve_ms')}ms"
            f" rerank={row.get('rerank_ms')}ms total={row.get('took_ms')}ms"
        )
    return 0


def _run_rows(kind: str, n: int, errors: bool) -> int:
    """The daemon's own work: an index pass, a watch batch, a sweep, a re-arm.

    One line per row and the fields left as they were written. A row here is
    read when something did not happen, and a formatter that drops a field is
    the formatter that drops the one that mattered.
    """
    rows = runledger.read(n, errors, kind)
    if not rows:
        print(f"no {kind} rows in {runledger.path()}")
        return 0
    for row in rows:
        when = time.strftime("%m-%d %H:%M:%S", time.localtime(row.pop("ts", 0)))
        kind = row.pop("kind", "-")
        rest = " ".join(f"{k}={v}" for k, v in row.items())
        print(f"{when} {kind} {rest}")
    return 0
