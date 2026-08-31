"""The disk the indexes occupy: what is on it, and how to give some back.

Split out of `registry.py` and `store.py`, and the seam is real: nothing here
reads `projects.json` and nothing here knows the schema. `registry` holds the
lock and hands this module the claimed set; `store` hands it a connection.

Named `disk` rather than `stores` on purpose -- a module one letter from
`store.py` is a name that gets typed wrong.

`refuse_on_shape` lives here because it is a statement about the tree, not about
the rows: both fleet-wide index wipes returned a verdict whose *shape* was the
tell, and neither was catchable by looking at any one store.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from . import config, quarantine

log = logging.getLogger(__name__)


def device(path: Path | str) -> int:
    """Which filesystem answers this path, or 0 where nothing does."""
    try:
        return Path(path).stat().st_dev
    except OSError:
        return 0


def all_stores() -> list[Path]:
    """Every store directory under `INDEX_DIR`, quarantine excluded.

    `.trash/` lives under `INDEX_DIR` and no row names it, so counting it as a
    store would have the reaper delete its own undo on the next pass -- and
    report the deletion as reclaimed waste.
    """
    if not config.INDEX_DIR.is_dir():
        return []
    return [p for p in config.INDEX_DIR.iterdir() if p.is_dir() and p.name != quarantine.DIR_NAME]


def unclaimed(claimed: set[Path]) -> list[Path]:
    """Store directories the caller's claimed set does not name."""
    return sorted(p for p in all_stores() if p not in claimed)


def idle_for(store_dir: Path, seconds: float) -> bool:
    newest = max((f.stat().st_mtime for f in store_dir.rglob("*")), default=0.0)
    return time.time() - newest >= seconds


def refuse_on_shape(stale: list[Path], row_count: int, *, force: bool = False) -> None:
    """Raise where the *shape* of a prune verdict says the input was wrong.

    Both fleet-wide index wipes in this engine's history returned a verdict that
    looked exactly like these two. An empty registry beside a full tree of
    stores is a registry that failed to load, not a fleet with nothing enrolled
    -- and `force` deliberately does not lift that one, because a human forcing
    a prune is answering "delete these", not "the registry is empty on purpose".
    """
    if not stale:
        return
    if not row_count:
        raise RuntimeError(
            f"refusing to prune {len(stale)} store(s) against an empty registry; "
            "load projects.json before pruning"
        )
    total = len(all_stores())
    if not force and len(stale) * 2 > total:
        raise RuntimeError(f"refusing to prune {len(stale)} of {total} store(s) without --force")


def reclaim(conn: sqlite3.Connection) -> None:
    """Give one store's freed pages back to the filesystem.

    Nothing ran this before, so a rebuild freed space inside the file and never
    outside it -- 205 MB stranded in the largest store on this fleet. In WAL
    mode the freed pages sit in the sidecar until a checkpoint truncates it, so
    the two statements are one answer. A store written before `auto_vacuum` was
    set carries 0 in its header and ignores the first; `compact` converts one.
    """
    try:
        conn.execute("PRAGMA incremental_vacuum")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as exc:
        log.warning("could not reclaim pages: %s", exc)


def compact(conn: sqlite3.Connection) -> None:
    """The one-time full VACUUM that converts an old store by rewriting it.

    Off every automatic path: a VACUUM needs free space equal to the file it
    rewrites, so it stays a command a human types with the number in front of
    them.
    """
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
