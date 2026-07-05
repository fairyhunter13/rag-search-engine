"""cAST structural-path header tests (CC1–CC6, arXiv 2506.15655).

CC1  chunk_file (chonkie path) prepends '# <rel>\\n' when project_root given
CC2  line-fallback path also carries the header
CC3  byte-identical re-chunk (determinism MR)
CC4  empty file returns [] — header not prepended to nothing
CC5  path outside project_root → basename fallback (no ValueError)
CC6  index_project stores chunks with the header in the vector store
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def test_cc1_header_prepended_chonkie_path(tmp_path):
    """CC1: chunk_file prepends '# <rel-path>\\n' when project_root given."""
    from rag_search.index.chunker import chunk_file
    root = tmp_path / "proj"
    root.mkdir()
    src = root / "src"
    src.mkdir()
    fpath = src / "main.py"
    content = "def foo(): pass\n" * 50
    fpath.write_text(content)
    chunks = chunk_file(fpath, content, "python", project_root=root)
    assert chunks, "CC1: no chunks produced"
    expected = "# src/main.py\n"
    for c in chunks:
        assert c.content.startswith(expected), (
            f"CC1: chunk missing header; starts: {c.content[:40]!r}"
        )


def test_cc2_linefallback_carries_header(tmp_path):
    """CC2: line-fallback path also carries the structural-path header."""
    from rag_search.index.chunker import chunk_file
    root = tmp_path / "proj"
    root.mkdir()
    fpath = root / "util.go"
    content = "\n".join(f"// line {i}" for i in range(200))
    fpath.write_text(content)
    chunks = chunk_file(fpath, content, "go", project_root=root)
    assert chunks, "CC2: no chunks produced"
    for c in chunks:
        assert c.content.startswith("# util.go\n"), (
            f"CC2: line-fallback chunk missing header; starts: {c.content[:50]!r}"
        )


def test_cc3_determinism_mr(tmp_path):
    """CC3 (MR): re-chunking identical content produces byte-identical chunks."""
    from rag_search.index.chunker import chunk_file
    root = tmp_path / "proj"
    root.mkdir()
    fpath = root / "service.py"
    content = "class OrderService:\n    def process(self, o): return o\n" * 20
    fpath.write_text(content)
    first = [c.content for c in chunk_file(fpath, content, "python", project_root=root)]
    second = [c.content for c in chunk_file(fpath, content, "python", project_root=root)]
    assert len(first) == len(second), (
        f"CC3: chunk count differs: {len(first)} vs {len(second)}"
    )
    assert first == second, (
        f"CC3: non-deterministic chunk content at index "
        f"{next(i for i, (a, b) in enumerate(zip(first, second, strict=True)) if a != b)}"
    )


def test_cc4_empty_file_returns_no_chunks(tmp_path):
    """CC4: empty file returns [] — header not prepended to nothing."""
    from rag_search.index.chunker import chunk_file
    root = tmp_path / "proj"
    root.mkdir()
    fpath = root / "empty.py"
    fpath.write_text("")
    chunks = chunk_file(fpath, "", "python", project_root=root)
    assert chunks == [], f"CC4: expected [], got {len(chunks)} chunks"


def test_cc5_path_outside_root_uses_basename(tmp_path):
    """CC5: path outside project_root falls back to basename (no ValueError raised)."""
    from rag_search.index.chunker import chunk_file
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "other" / "module.py"
    outside.parent.mkdir()
    content = "def x(): pass\n" * 20
    outside.write_text(content)
    chunks = chunk_file(outside, content, "python", project_root=root)
    assert chunks, "CC5: no chunks produced"
    for c in chunks:
        assert c.content.startswith("# module.py\n"), (
            f"CC5: outside-root fallback header wrong; starts: {c.content[:50]!r}"
        )


def test_cc6_indexer_stores_chunks_with_header(embedder, tmp_path_factory):
    """CC6: index_project stores chunks carrying the structural-path header."""
    from rag_search.index.indexer import index_project
    from rag_search.index.store import VectorStore
    root = tmp_path_factory.mktemp("cast_proj")
    (root / "api.py").write_text("def handle(req):\n    return {'ok': True}\n" * 25)
    vdb = tmp_path_factory.mktemp("cast_stores") / "v.db"
    vs = VectorStore(vdb)
    try:
        index_project(root, embedder, vs, federation_mode=False)
        rows = vs._con.execute("SELECT content FROM chunks LIMIT 10").fetchall()
    finally:
        vs.close()
    assert rows, "CC6: no chunks in vector store after index_project"
    for row in rows:
        assert row[0].startswith("# api.py\n"), (
            f"CC6: indexed chunk missing cAST header; starts: {row[0][:50]!r}"
        )
