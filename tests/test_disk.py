"""Giving pages back, and the pragma order that decides whether it works.

Nothing in this engine ever ran a VACUUM, so a rebuild, a new exclude or a mass
deletion freed space inside the file and never to the filesystem -- 205 MB
stranded in the largest store on this fleet when it was measured.

A VACUUM is not the whole answer either. `sqlite-vec` stores its vectors in
blocks of 1024 slots and drops a block only when every slot in it is free. Real
deletes are scattered, so each block keeps a live row and stays -- and a KNN
reads every block whole. The dead slots are inside blob rows, where the
freelist cannot see them.
"""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

from coderag import config, conns, disk, store

# vec0's block size, which it does not expose and which decides every count below.
_VEC_BLOCK = 1024


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


def test_a_store_written_before_4_4_needs_the_vacuum_reclaim_cannot_do(tmp_path):
    """Why `doctor --compact` exists beside `reclaim`, and why it is hand-typed.

    Every store on the fleet predates the pragma, and `auto_vacuum` lives in a
    header that is already written. So `reclaim` is a silent no-op on all of
    them and only a full VACUUM converts one -- at the cost of rewriting the
    file, which is the number a human is meant to see before typing it.
    """
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    # The order a pre-4.4 store was built in: the journal mode writes a header,
    # and `auto_vacuum` reads back NONE ever after.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
    _fill(conn)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    grown = path.stat().st_size

    with conn:
        conn.execute("DELETE FROM meta WHERE key LIKE 'k1%'")
    kept = conn.execute("SELECT count(*) FROM meta").fetchone()[0]
    disk.reclaim(conn)

    assert path.stat().st_size == grown, "reclaim shrank a store with no INCREMENTAL header"

    disk.compact(conn)

    assert path.stat().st_size < grown
    assert conn.execute("SELECT count(*) FROM meta").fetchone()[0] == kept
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2


def _churned(tmp_path, name: str):
    """Three vec0 blocks holding one block's worth of live rows, scattered.

    Scattered is the whole fixture. A delete that empties a block whole gives
    it back, so a contiguous tail delete leaves nothing to repack and the arms
    below pass against code that does nothing. What a watched project does is
    rewrite a few chunks anywhere, which leaves every block partly live.
    """
    project = tmp_path / name
    project.mkdir()
    conn = store.connect(project)
    one = [0.01] * config.EMBED_DIMS
    with conn:
        conn.executemany(
            "INSERT INTO chunks_vec(chunk_id, embedding) VALUES(?, ?)",
            [(i, struct.pack(f"{config.EMBED_DIMS}f", *one)) for i in range(1, 3 * _VEC_BLOCK + 1)],
        )
        conn.execute("DELETE FROM chunks_vec WHERE chunk_id % 4 != 0")
    return conn


def test_a_vacuum_leaves_every_vector_block_where_it_was(tmp_path):
    """The negative arm, and the reason the repack had to be written.

    `--compact` was a VACUUM alone for its whole life. A KNN reads a block
    whole, so the one number that decides what a search pays never moved: 249
    blocks before and 249 after on the largest store here, and 59 MiB back out
    of 689 MiB.
    """
    _reset()
    conn = _churned(tmp_path, "vacuumed")
    before, _ = store.vector_blocks(conn)
    assert before == 3

    disk.compact(conn)

    assert store.vector_blocks(conn)[0] == before


def test_a_repack_drops_the_blocks_a_delete_emptied(tmp_path):
    _reset()
    conn = _churned(tmp_path, "repacked")

    assert store.repack_vectors(conn) == (3, 1)
    assert conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] == 768


def test_a_repack_keeps_every_vector_it_read(tmp_path):
    """These rows are the only copy, so an id or a vector lost here is lost."""
    _reset()
    conn = _churned(tmp_path, "intact")
    was = {r["chunk_id"]: r["embedding"] for r in conn.execute("SELECT * FROM chunks_vec")}

    store.repack_vectors(conn)

    assert {r["chunk_id"]: r["embedding"] for r in conn.execute("SELECT * FROM chunks_vec")} == was
