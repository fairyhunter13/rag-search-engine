"""sqlite-vec vector store for code chunk embeddings."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import sqlite_vec


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
    con.commit()
    return con


class VectorStore:
    """sqlite-vec backed vector store for code chunk embeddings (float32 ANN)."""

    def __init__(self, db_path: Path, dim: int = 768):
        self._con = _open(db_path, dim)

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

    def close(self) -> None:
        self._con.close()
