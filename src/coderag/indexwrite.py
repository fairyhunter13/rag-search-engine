"""The store-writing half of a pass: chunk, embed, upsert, commit.

Split out of `index.py` at the 300-line ceiling. `index.py` keeps the queue,
the worker and the diff; this keeps everything that touches the connection.
"""

from __future__ import annotations

from pathlib import Path

from . import chunk as chunker
from . import embed, filters, progress, store

# Files per transaction. One commit per file fsyncs 61,714 times; one commit per
# project leaves a multi-hour build with nothing durable if the daemon stops.
BATCH_FILES = 64


def _relang(conn) -> int:
    """Re-derive `lang` for stored files whose path now maps somewhere else.

    Without this a `LANGS` or `FILENAMES` addition reaches only files that
    happen to change afterwards: the content-hash diff sees no difference, so
    2,058 `.groovy` files stay classified as nothing.
    """
    stale = [
        (fresh, path)
        for path, was in store.file_langs(conn).items()
        if (fresh := filters.lang_of(path)) != was
    ]
    if stale:
        store.set_langs(conn, stale)
        conn.commit()
    return len(stale)


def _wipe(conn) -> None:
    for table in ("chunks_fts", "chunks_vec", "chunks", "files"):
        conn.execute(f"DELETE FROM {table}")


def _write_files(conn, metas: list, project: Path | str = "") -> int:
    """Chunk, embed and store, committing every BATCH_FILES.

    The embed call sits outside the transaction on purpose: it is the slow part
    and it takes the GPU lock, and holding a SQLite write transaction across it
    would block every reader for the length of a batch.
    """
    written, pending = 0, []
    progress.begin(project, len(metas))
    for meta in metas:
        chunks = chunker.chunk_text(meta.text, rel_path=meta.rel)
        if chunks:
            vectors = embed.get_embedder().embed(
                [c.embed_text for c in chunks], side=embed.DOCUMENT
            )
            pending.append((meta, chunks, vectors))
            if len(pending) >= BATCH_FILES:
                written += _flush(conn, pending)
                pending = []
        progress.advance()
    written += _flush(conn, pending)
    progress.finish()
    return written


def _flush(conn, pending: list) -> int:
    for meta, chunks, vectors in pending:
        store.upsert_file(
            conn,
            {
                "path": meta.rel,
                "mtime": meta.mtime,
                "size": meta.size,
                "sha256": meta.sha256,
                "lang": meta.lang,
                "n_lines": meta.n_lines,
            },
            chunks,
            vectors,
        )
    if pending:
        conn.commit()
    return len(pending)
