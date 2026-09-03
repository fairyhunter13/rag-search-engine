"""One SQLite file per project: rows, BM25 and vectors in the same store.

The rowid contract, stated once, here, because three tables depend on it and
only one of them enforces anything:

    chunks.id  ==  chunks_fts rowid  ==  chunks_vec.chunk_id

`chunks_fts` is an external-content table over `chunks`, and `chunks_vec` is a
vec0 virtual table. **Neither participates in foreign-key cascade.** Deleting a
row from `chunks` leaves both of them holding it, and the symptom is not an
error -- it is a search result pointing at a line range that no longer exists,
which reads exactly like a working engine. So every delete touches all three
explicitly, in one transaction.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import sqlite_vec

from . import config
from .conns import cache
from .lexical import identifier_tokens

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id      INTEGER PRIMARY KEY,
  path    TEXT NOT NULL UNIQUE,
  mtime   REAL NOT NULL,
  size    INTEGER NOT NULL,
  sha256  TEXT NOT NULL,
  lang    TEXT NOT NULL DEFAULT '',
  n_lines INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chunks (
  id         INTEGER PRIMARY KEY,
  file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  ord        INTEGER NOT NULL,
  start_line INTEGER NOT NULL,
  end_line   INTEGER NOT NULL,
  n_chars    INTEGER NOT NULL,
  sha256     TEXT NOT NULL,
  text       TEXT NOT NULL,
  tokens     TEXT NOT NULL DEFAULT '',
  header     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);

-- `header` is its own column rather than being folded into `text`: the body is
-- what a preview shows and what a line range must resolve to, so a path
-- prepended to it would appear in every result and add the same terms to every
-- BM25 score. Its own column is searchable and invisible.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, tokens, header, content='chunks', content_rowid='id',
  tokenize="unicode61 remove_diacritics 0 tokenchars '_$.'"
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _vec_ddl() -> str:
    return (
        "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0("
        f"chunk_id INTEGER PRIMARY KEY, embedding FLOAT[{config.EMBED_DIMS}] "
        "distance_metric=cosine)"
    )


def connect(project: Path | str, *, create: bool = True) -> sqlite3.Connection:
    """A thread-local handle for one project's store.

    Thread-local because SQLite connections are not shareable across threads
    and this daemon has five: sharing one is an intermittent
    ProgrammingError under load, which is the worst possible failure shape.
    `conns` owns the lifetime of what this returns.
    """
    path = config.index_path(project)
    key = str(path)
    live = cache()
    with live.lock:
        if key in live.conns:
            return live.conns[key]

    if not create and not path.exists():
        raise FileNotFoundError(f"no index for {project}")
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(key, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    # First, and before WAL: `auto_vacuum` is written into the file header, so
    # it only takes on a database that has none yet. Set after `journal_mode` it
    # reads back 0, with no error.
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(f"PRAGMA cache_size=-{config.SQLITE_CACHE_KIB}")
    conn.executescript(SCHEMA)
    conn.execute(_vec_ddl())
    conn.commit()
    with live.lock:
        live.conns[key] = conn
    return conn


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_meta(conn: sqlite3.Connection, **pairs) -> None:
    conn.executemany(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [(k, json.dumps(v)) for k, v in pairs.items()],
    )


def stamp(conn: sqlite3.Connection) -> None:
    set_meta(
        conn,
        embed_model=config.EMBED_MODEL,
        embed_dims=config.EMBED_DIMS,
        chunk_chars=config.CHUNK_CHARS,
        chunk_overlap=config.CHUNK_OVERLAP,
        chunk_header_path=config.CHUNK_HEADER_PATH,
        chunk_md_splitter=config.CHUNK_MD_SPLITTER,
        chunk_algo=config.CHUNK_ALGO,
    )


def incompatible(conn: sqlite3.Connection) -> str | None:
    """Why this store cannot be read with the current settings, if it cannot.

    A vector written by another model is not wrong in any way SQLite can see;
    it is simply in a different space, and mixing two spaces in one table
    produces rankings that look plausible and are noise. Returning the reason
    rather than a bool is so the rebuild can say what changed.
    """
    if get_meta(conn, "embed_model") is None:
        return None  # never stamped: an empty store, not an incompatible one
    for key, now in (
        ("embed_model", config.EMBED_MODEL),
        ("embed_dims", config.EMBED_DIMS),
        ("chunk_chars", config.CHUNK_CHARS),
        ("chunk_overlap", config.CHUNK_OVERLAP),
        ("chunk_header_path", config.CHUNK_HEADER_PATH),
        ("chunk_md_splitter", config.CHUNK_MD_SPLITTER),
        ("chunk_algo", config.CHUNK_ALGO),
    ):
        was = get_meta(conn, key)
        if was != now:
            return f"{key} changed: {was!r} -> {now!r}"
    return None


def file_digests(conn: sqlite3.Connection) -> dict[str, str]:
    """path -> sha256 for every indexed file. The whole of the change diff."""
    return {r["path"]: r["sha256"] for r in conn.execute("SELECT path, sha256 FROM files")}


def file_langs(conn: sqlite3.Connection) -> dict[str, str]:
    """path -> lang. Read back because `lang` is derived and then stored."""
    return {r["path"]: r["lang"] for r in conn.execute("SELECT path, lang FROM files")}


def set_langs(conn: sqlite3.Connection, pairs: list[tuple[str, str]]) -> int:
    """(lang, path) pairs. Touches no chunk, so it costs no embedding."""
    conn.executemany("UPDATE files SET lang = ? WHERE path = ?", pairs)
    return len(pairs)


def delete_files(conn: sqlite3.Connection, paths: list[str]) -> int:
    """Remove files and every trace of their chunks from all three tables.

    The two virtual tables are deleted from explicitly: neither honours the FK
    cascade, and what they leave behind is a hit on a line range that no longer
    exists.
    """
    if not paths:
        return 0
    marks = ",".join("?" * len(paths))
    ids = [
        r["id"]
        for r in conn.execute(
            f"SELECT c.id FROM chunks c JOIN files f ON f.id = c.file_id WHERE f.path IN ({marks})",
            paths,
        )
    ]
    if ids:
        chunk_marks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({chunk_marks})", ids)
        conn.execute(f"DELETE FROM chunks_vec WHERE chunk_id IN ({chunk_marks})", ids)
    conn.execute(f"DELETE FROM files WHERE path IN ({marks})", paths)
    return len(ids)


def upsert_file(conn: sqlite3.Connection, meta: dict, chunks: list, embeddings) -> int:
    """Replace one file and its chunks across all three tables, atomically.

    Delete-then-insert rather than a diff: chunk boundaries move when a line is
    added near the top of a file, so almost every edit renumbers most of the
    chunks anyway, and a partial update would have to reconcile three tables to
    save one insert.

    The caller holds the transaction. Committing per file would fsync once per
    file across a 61,714-file run, and leaves a half-written file visible to a
    concurrent query in between.
    """
    delete_files(conn, [meta["path"]])
    cur = conn.execute(
        "INSERT INTO files(path, mtime, size, sha256, lang, n_lines) VALUES(?,?,?,?,?,?)",
        (
            meta["path"],
            meta["mtime"],
            meta["size"],
            meta["sha256"],
            meta.get("lang", ""),
            meta.get("n_lines", 0),
        ),
    )
    file_id = cur.lastrowid

    for i, chunk in enumerate(chunks):
        tokens = identifier_tokens(chunk.text)
        header = getattr(chunk, "header", "")
        cur = conn.execute(
            "INSERT INTO chunks(file_id, ord, start_line, end_line, n_chars, sha256, text,"
            " tokens, header) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                file_id,
                chunk.ord,
                chunk.start_line,
                chunk.end_line,
                chunk.n_chars,
                chunk.sha256,
                chunk.text,
                tokens,
                header,
            ),
        )
        chunk_id = cur.lastrowid
        # The contract at the top of this file, honoured in three statements.
        conn.execute(
            "INSERT INTO chunks_fts(rowid, text, tokens, header) VALUES(?,?,?,?)",
            (chunk_id, chunk.text, tokens, header),
        )
        if embeddings is not None:
            conn.execute(
                "INSERT INTO chunks_vec(chunk_id, embedding) VALUES(?,?)",
                (chunk_id, embeddings[i].astype("float32").tobytes()),
            )
    return len(chunks)


def orphans(conn: sqlite3.Connection) -> dict[str, int]:
    """Rows in the virtual tables with no chunk behind them.

    This is `doctor`'s only structural check, and it is the one that catches a
    delete path that forgot a table -- the failure whose symptom is a plausible
    search result pointing at nothing.
    """
    fts = conn.execute(
        "SELECT COUNT(*) AS n FROM chunks_fts WHERE rowid NOT IN (SELECT id FROM chunks)"
    ).fetchone()["n"]
    vec = conn.execute(
        "SELECT COUNT(*) AS n FROM chunks_vec WHERE chunk_id NOT IN (SELECT id FROM chunks)"
    ).fetchone()["n"]
    return {"fts": fts, "vec": vec}


def counts(conn: sqlite3.Connection) -> tuple[int, int]:
    files = conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
    chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
    return files, chunks


def vector_blocks(conn: sqlite3.Connection) -> tuple[int, int]:
    """Blocks the vec0 table holds, and the bytes in them.

    A KNN reads every block whole, so this is what a search pays and the live
    row count is not. On the largest store here: 249 blocks holding 747 MiB,
    for 120 MiB of live vectors.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(vectors)), 0) AS b "
        "FROM chunks_vec_vector_chunks00"
    ).fetchone()
    return row["n"], row["b"]


def repack_vectors(conn: sqlite3.Connection) -> tuple[int, int]:
    """Rewrite `chunks_vec` so its blocks hold live rows only. Blocks before, after.

    vec0 keeps its vectors in blocks of 1024 slots and gives a block back only
    when every slot in it is free. A watched project rewrites a few chunks
    anywhere, all day, so the dead slots land scattered: every block keeps a
    live row, every block stays, and every KNN reads all of them whole. The
    table grows while the row count does not -- 6.3x on the store re-indexed
    most, 2.4x across the fleet, 40745 live rows in 249 blocks of 1024.
    `VACUUM` cannot reach it, because those slots sit inside blob rows and not
    in the freelist: it gave back 59 MiB of 689 MiB and moved no block.

    These rows are the only copy of the vectors, so the read has to agree with
    the table before the DROP. A short read that reached it would delete
    vectors nothing but a full re-index could put back.
    """
    before, _ = vector_blocks(conn)
    rows = conn.execute("SELECT chunk_id, embedding FROM chunks_vec").fetchall()
    expected = conn.execute("SELECT COUNT(*) AS n FROM chunks_vec").fetchone()["n"]
    if len(rows) != expected:
        raise RuntimeError(f"read {len(rows)} of {expected} vectors, refusing to repack")
    with conn:
        conn.execute("DROP TABLE chunks_vec")
        conn.execute(_vec_ddl())
        conn.executemany(
            "INSERT INTO chunks_vec(chunk_id, embedding) VALUES(?, ?)",
            [(row["chunk_id"], row["embedding"]) for row in rows],
        )
    after, _ = vector_blocks(conn)
    return before, after
