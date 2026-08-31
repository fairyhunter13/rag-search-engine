"""Giving pages back, and the pragma order that decides whether it works.

Nothing in this engine ever ran a VACUUM, so a rebuild, a new exclude or a mass
deletion freed space inside the file and never to the filesystem -- 205 MB
stranded in the largest store on this fleet when it was measured.
"""

from __future__ import annotations

from pathlib import Path

from coderag import conns, disk, store


def _reset() -> None:
    conns.close_all()
    with conns._caches_lock:
        conns._caches.clear()
    conns._local.__dict__.pop("cache", None)


def _fill(conn, rows: int = 400) -> None:
    with conn:
        conn.executemany(
            "INSERT INTO meta(key, value) VALUES(?, ?)",
            [(f"k{i}", '"' + "v" * 4000 + '"') for i in range(rows)],
        )


def test_auto_vacuum_is_set_before_the_journal_mode(tmp_path):
    """`auto_vacuum` is written into the file header, so it only takes on a
    database that has none yet. Set after `journal_mode=WAL` -- which writes
    one -- the pragma is a silent no-op that reads back 0."""
    _reset()
    project = tmp_path / "fresh"
    project.mkdir()
    conn = store.connect(project)

    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2  # INCREMENTAL


def test_a_store_gives_its_freed_pages_back(tmp_path):
    """The whole of 4.4: without `reclaim` the file never shrinks."""
    _reset()
    project = tmp_path / "roomy"
    project.mkdir()
    conn = store.connect(project)
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    _fill(conn)
    # Checkpointed first: in WAL mode the pages are in the sidecar until then,
    # so an uncheckpointed size reads as the empty file it started as.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    grown = Path(path).stat().st_size

    with conn:
        conn.execute("DELETE FROM meta")
    disk.reclaim(conn)

    assert Path(path).stat().st_size < grown


def test_reclaim_never_raises_at_a_caller_that_cannot_answer(tmp_path):
    """It runs on the index pass. A store that refuses the pragma must cost the
    pass a log line, never the whole result the user asked for."""
    _reset()
    project = tmp_path / "closed"
    project.mkdir()
    conn = store.connect(project)
    conn.close()

    disk.reclaim(conn)  # no raise
