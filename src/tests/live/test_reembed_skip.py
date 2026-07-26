"""Live proof gates for the byte-identical re-embed skip (real embedder, no mocks).

The watcher fires on writes, not on content changes: doc generators, formatters and
`git checkout` routinely rewrite a file with the bytes it already had. index_files used to
delete and re-embed every path it was handed, so those rewrites cost full GPU + tokenizer
work for a bit-identical vector. Measured on redacted-name-10-project's encyclopedia: one derive run
rewrote 136 files, of which exactly 1 had different content.

RS1  a byte-identical rewrite must not touch the stored chunks at all
RS2  a real content change must still reindex (the skip must never blind the watcher)
RS3  a deleted file must still be purged (the skip must not swallow deletions)
RS4  a stale hash from another embed signature must not be trusted
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def _store_rows(vs, path: str) -> list[tuple]:
    return vs._con.execute(
        "SELECT chunk_id, content FROM chunks WHERE path=? ORDER BY start_line", (path,)
    ).fetchall()


def _index(files, root, vs):
    from rag_search.embed.embedder import get_embedder
    from rag_search.index.indexer import index_files
    return index_files(files, get_embedder(), vs, project_root=root)


def test_rs1_identical_rewrite_skips_reembed(safe_tmp_path):
    """RS1: rewriting a file with identical bytes must leave its chunks untouched.

    Output alone cannot discriminate here -- re-embedding produces the same chunks, so a
    test that only compared search results would pass with the skip deleted. Instead we
    tamper with the stored content after the first index: if the second call re-embeds, it
    deletes and rewrites the row and the tamper marker disappears. The marker surviving is
    proof that no delete/embed/insert happened for that file.
    """
    from rag_search.index.store import VectorStore

    root = safe_tmp_path
    src = root / "mod.py"
    src.write_text("def alpha():\n    return 1\n")
    vs = VectorStore(root / "vectors.db")
    try:
        _index([src], root, vs)
        rows = _store_rows(vs, str(src))
        assert rows, "RS1: first index must store chunks"
        marker = "SENTINEL-NOT-REEMBEDDED"
        vs._con.execute(
            "UPDATE chunks SET content=? WHERE chunk_id=?", (marker, rows[0][0])
        )
        vs.flush()

        src.write_text("def alpha():\n    return 1\n")  # same bytes, new mtime
        _index([src], root, vs)

        after = _store_rows(vs, str(src))
        assert [r[0] for r in after] == [r[0] for r in rows], (
            "RS1: chunk ids changed -- the file was deleted and re-embedded"
        )
        assert after[0][1] == marker, (
            "RS1: tamper marker was overwritten, so the identical rewrite re-embedded"
        )
    finally:
        vs.close()


def test_rs2_changed_content_still_reindexes(safe_tmp_path):
    """RS2: a real edit must still be embedded -- the skip must not blind the watcher."""
    from rag_search.index.store import VectorStore

    root = safe_tmp_path
    src = root / "mod.py"
    src.write_text("def alpha():\n    return 1\n")
    vs = VectorStore(root / "vectors.db")
    try:
        _index([src], root, vs)
        src.write_text("def alpha():\n    return 1\n\n\ndef beta_unique_marker():\n    return 2\n")
        _index([src], root, vs)
        stored = " ".join(r[1] for r in _store_rows(vs, str(src)))
        assert "beta_unique_marker" in stored, (
            "RS2: edited content never reached the store -- the skip swallowed a real change"
        )
    finally:
        vs.close()


def test_rs3_deleted_file_still_purged(safe_tmp_path):
    """RS3: an unreadable/deleted path must still drop its chunks and its hash row."""
    from rag_search.index.store import VectorStore

    root = safe_tmp_path
    src = root / "gone.py"
    src.write_text("def gamma():\n    return 3\n")
    vs = VectorStore(root / "vectors.db")
    try:
        _index([src], root, vs)
        assert _store_rows(vs, str(src)), "RS3: setup failed, nothing indexed"
        src.unlink()
        _index([src], root, vs)
        assert not _store_rows(vs, str(src)), "RS3: chunks for a deleted file were not purged"
        assert vs.file_hash(str(src)) is None, "RS3: hash row outlived the file's chunks"
    finally:
        vs.close()


def test_rs4_hash_is_scoped_to_the_embed_signature(safe_tmp_path):
    """RS4: a hash recorded under a different pipeline must not authorise a skip.

    Without this, changing EMBED_MODEL or EMBED_MAX_TOKENS would leave every file looking
    unchanged, and the migration that re-embeds the fleet would silently do nothing.
    """
    from rag_search.index.indexer import _content_hash
    from rag_search.index.store import VectorStore

    root = safe_tmp_path
    src = root / "mod.py"
    src.write_text("def alpha():\n    return 1\n")
    vs = VectorStore(root / "vectors.db")
    try:
        _index([src], root, vs)
        recorded = vs.file_hash(str(src))
        assert recorded == _content_hash(src.read_text()), (
            "RS4: recorded hash does not match the current pipeline's hash"
        )
        import hashlib
        content_only = hashlib.sha256(src.read_text().encode()).hexdigest()
        assert recorded != content_only, (
            "RS4: hash ignores the embed signature, so a model/budget change would be "
            "invisible and the re-embed migration would skip every file"
        )
    finally:
        vs.close()
