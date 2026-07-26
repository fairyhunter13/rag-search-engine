"""sqlite-vec vector store for code chunk embeddings."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import sqlite_vec

from rag_search.core.config import EMBED_MAX_TOKENS, EMBED_MODEL

# Bump by hand whenever chunk *shape* changes (boundaries, headers, overlap) in a
# way that makes old vectors incomparable to new ones.
CHUNKER_REV = "cast-1"


def embed_signature(dim: int = 768) -> str:
    """Identity of the pipeline that produced a set of vectors.

    A stored vector is only comparable to a query embedded the same way, so any
    change here invalidates the whole index. Recording it is what lets a stale
    index announce itself: without it, a config change leaves old and new chunk
    shapes coexisting silently and forever — which is how a 512-token truncation
    went unnoticed while discarding half of every indexed repo.
    """
    return f"{EMBED_MODEL}|{EMBED_MAX_TOKENS}|{dim}|{CHUNKER_REV}"


def _open(db_path: Path, dim: int) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), check_same_thread=False)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            chunk_id   INTEGER PRIMARY KEY,
            path       TEXT NOT NULL,
            start_line INTEGER,
            end_line   INTEGER,
            language   TEXT,
            content    TEXT
        )
    """)
    con.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
            chunk_id  INTEGER PRIMARY KEY,
            embedding FLOAT[{dim}]
        )
    """)
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    # Content hash of each file whose chunks are currently embedded here, so an incremental
    # reindex can skip a file that was rewritten with identical bytes (generators and
    # save-on-format rewrite constantly; embedding them again buys nothing).
    con.execute(
        "CREATE TABLE IF NOT EXISTS file_hashes (path TEXT PRIMARY KEY, hash TEXT NOT NULL)"
    )
    con.commit()
    return con


class VectorStore:
    """sqlite-vec backed vector store for code chunk embeddings (float32 ANN)."""

    def __init__(self, db_path: Path, dim: int = 768):
        self._con = _open(db_path, dim)
        self._dim = dim

    def stamp(self) -> None:
        """Record which pipeline built the vectors now held here. Call after a full reindex."""
        self._con.execute(
            "INSERT OR REPLACE INTO meta VALUES ('embed_signature', ?)",
            (embed_signature(self._dim),),
        )

    def stale_signature(self) -> str | None:
        """The recorded signature, if it disagrees with the running config; else None.

        A populated index with no stamp predates stamping, so it is stale too.
        An empty index is never stale — there is nothing to be inconsistent with.
        """
        row = self._con.execute(
            "SELECT value FROM meta WHERE key='embed_signature'"
        ).fetchone()
        if row is None:
            return "<unstamped>" if self.count() else None
        return None if row[0] == embed_signature(self._dim) else row[0]

    def insert(
        self, chunk_id: int, path: str, start: int, end: int,
        language: str, content: str, vector: np.ndarray,
    ) -> None:
        v = vector.astype(np.float32).tobytes()
        self._con.execute(
            "INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?)",
            (chunk_id, path, start, end, language, content),
        )
        self._con.execute(
            "INSERT OR REPLACE INTO vec_chunks(chunk_id, embedding) VALUES (?,?)",
            (chunk_id, v),
        )

    def file_hash(self, path: str) -> str | None:
        """The content hash whose chunks are currently embedded for path, if any."""
        row = self._con.execute(
            "SELECT hash FROM file_hashes WHERE path=?", (path,)
        ).fetchone()
        return row[0] if row else None

    def set_file_hash(self, path: str, digest: str) -> None:
        """Record that path's chunks in this store were built from content hashing to digest.

        Only ever written after that file's chunks are inserted, and dropped by
        delete_by_path/clear, so a hash row always describes what is really stored.
        """
        self._con.execute(
            "INSERT OR REPLACE INTO file_hashes VALUES (?,?)", (path, digest)
        )

    def flush(self) -> None:
        self._con.commit()

    def search(self, query_vector: np.ndarray, top_k: int = 10) -> list[dict]:
        v = query_vector.astype(np.float32).tobytes()
        rows = self._con.execute(
            """
            SELECT c.chunk_id, c.path, c.start_line, c.end_line,
                   c.language, c.content, v.distance
            FROM vec_chunks v
            JOIN chunks c USING (chunk_id)
            WHERE v.embedding MATCH ? AND v.k = ?
            ORDER BY v.distance
            """,
            [v, top_k],
        ).fetchall()
        return [
            {"chunk_id": r[0], "path": r[1], "start_line": r[2], "end_line": r[3],
             "language": r[4], "content": r[5], "score": float(1.0 - r[6])}
            for r in rows
        ]

    def count(self) -> int:
        return self._con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def clear(self) -> None:
        """Drop all chunk metadata + vectors (for idempotent full reindex)."""
        self._con.execute("DELETE FROM vec_chunks")
        self._con.execute("DELETE FROM chunks")
        self._con.execute("DELETE FROM file_hashes")

    def delete_by_path(self, path: str) -> None:
        """Remove all chunks (metadata + vectors) for a single file path."""
        ids = [r[0] for r in self._con.execute("SELECT chunk_id FROM chunks WHERE path=?", (path,))]
        for cid in ids:
            self._con.execute("DELETE FROM vec_chunks WHERE chunk_id=?", (cid,))
        self._con.execute("DELETE FROM chunks WHERE path=?", (path,))
        self._con.execute("DELETE FROM file_hashes WHERE path=?", (path,))

    def close(self) -> None:
        self._con.close()
