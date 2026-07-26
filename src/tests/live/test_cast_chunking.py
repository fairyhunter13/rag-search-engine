"""cAST structural-path header tests (CC1–CC6, arXiv 2506.15655).

CC1  chonkie AST path actually runs (not the fallback) + header + real line numbers
CC2  line-fallback path also carries the header
CC3  byte-identical re-chunk (determinism MR)
CC4  empty file returns [] — header not prepended to nothing
CC5  path outside project_root → basename fallback (no ValueError)
CC6  index_project stores chunks with the header in the vector store
CC7  chonkie accepts the kwargs chunk_file actually passes
CC8  chunks fit the embedder's token budget (nothing truncated away)
CC9  a full index stamps its embed signature, and drift is detectable
CC10 migrating drifted vectors re-embeds without destroying the graph
CC11 an unsupported language falls back cheaply, never by brute-force parsing
CC12 the CodeChunker is built once per language, not once per file
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def test_cc1_chonkie_path_actually_runs(tmp_path):
    """CC1: the AST chunker runs — the line-window fallback must not be reached.

    The previous assertion only checked the '# <rel>' header, which *both*
    branches prepend, so it stayed green while chonkie raised TypeError on every
    single call and 100% of chunks came from blind 100-line windows.

    The discriminator is real output, not a mock: when chonkie fails, chunk_file
    returns the fallback's chunks verbatim, so the two span lists are identical.
    Comparing against _line_chunks directly cannot be confused by _fit_budget
    re-splitting an oversized AST node (which reintroduces overlap legitimately).
    """
    from rag_search.index.chunker import _line_chunks, chunk_file
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    fpath = root / "src" / "main.py"
    content = "".join(
        f"class C{i}:\n    def m{i}(self, a, b):\n        return a + b + {i}\n\n"
        for i in range(60)
    )
    fpath.write_text(content)
    chunks = chunk_file(fpath, content, "python", project_root=root)
    spans = [(c.start_line, c.end_line) for c in chunks]
    fallback = [(c.start_line, c.end_line) for c in _line_chunks(content, str(fpath), "python")]
    assert len(chunks) > 1, f"CC1: need >1 chunk to compare, got {len(chunks)}"
    assert spans != fallback, (
        f"CC1: chunk_file returned exactly the line-window fallback's spans {spans} "
        f"— chonkie did not run"
    )
    for c in chunks:
        assert c.content.startswith("# src/main.py\n"), (
            f"CC1: chunk missing header; starts: {c.content[:40]!r}"
        )
    # chonkie reports *character* offsets; storing them raw would put a 5674 in a
    # field every search result prints as `path:line`.
    assert max(c.end_line for c in chunks) <= content.count("\n") + 1, (
        "CC1: end_line exceeds the file's line count — character offsets leaked in"
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


def test_cc7_chonkie_accepts_the_kwargs_we_pass():
    """CC7: constructing CodeChunker the way chunk_file does must not raise.

    Guards Finding 1 at its root: `chunk_overlap=` is not in chonkie's signature,
    and a bare `except Exception: pass` turned that TypeError into silence.
    """
    from chonkie import CodeChunker

    from rag_search.core.config import EMBED_MAX_TOKENS
    from rag_search.index.chunker import _tokenizer
    CodeChunker(tokenizer=_tokenizer(), chunk_size=EMBED_MAX_TOKENS, language="python")


def test_cc8_chunks_fit_the_embedder_token_budget():
    """CC8: ≥95% of chunks from real source must fit EMBED_MAX_TOKENS.

    Direct guard for Finding 2. With 100-line windows against a 512-token cap,
    78.4% of chunks overflowed and 51% of all tokens were never embedded at all —
    a chunk larger than the window that embeds it loses its tail silently.
    """
    from pathlib import Path

    from rag_search.core.config import EMBED_MAX_TOKENS
    from rag_search.index.chunker import _tokenizer, chunk_file
    root = Path(__file__).resolve().parents[2] / "rag_search"
    files = sorted(root.rglob("*.py"))[:40]
    assert files, f"CC8: no source files found under {root}"
    tok = _tokenizer()
    chunks = [
        c for f in files
        for c in chunk_file(f, f.read_text(), "python", project_root=root)
    ]
    over = [c for c in chunks if len(tok.encode(c.content).ids) > EMBED_MAX_TOKENS]
    assert len(over) / len(chunks) <= 0.05, (
        f"CC8: {len(over)}/{len(chunks)} chunks ({len(over) / len(chunks):.1%}) exceed "
        f"EMBED_MAX_TOKENS={EMBED_MAX_TOKENS}; their tails never reach the index"
    )


def test_cc9_index_records_its_embed_signature(embedder, tmp_path_factory):
    """CC9: a full index stamps the pipeline that built it, and drift is detectable.

    Without the stamp, changing the model or token budget leaves old and new chunk
    shapes coexisting in one index with nothing able to tell them apart.
    """
    from rag_search.index.indexer import index_project
    from rag_search.index.store import VectorStore
    root = tmp_path_factory.mktemp("stamp_proj")
    (root / "api.py").write_text("def handle(req):\n    return {'ok': True}\n" * 25)
    vs = VectorStore(tmp_path_factory.mktemp("stamp_stores") / "v.db")
    try:
        index_project(root, embedder, vs, federation_mode=False)
        assert vs.stale_signature() is None, "CC9: a freshly built index reports itself stale"
        vs._con.execute(
            "INSERT OR REPLACE INTO meta VALUES ('embed_signature','old|512|768|pre')"
        )
        assert vs.stale_signature() == "old|512|768|pre", (
            "CC9: a config change must be detectable, not silent"
        )
    finally:
        vs.close()


def test_cc11_unsupported_language_falls_back_cheaply(tmp_path):
    """CC11: a language chonkie has no grammar for must not cost per-file parsing.

    Recovering from chonkie's raise by retrying with language="auto" makes it parse
    the file against every grammar it owns — 0.50s/file against 0.02s named, and
    worse on bigger files. That is invisible in output (the chunks are identical
    either way) and only shows up as a fleet reindex that never finishes, so the
    assertion has to be on time, not on chunks.
    """
    import time

    from rag_search.core.config import EMBED_MAX_TOKENS
    from rag_search.index.chunker import _code_chunker, chunk_file
    root = tmp_path / "proj"
    root.mkdir()
    assert _code_chunker("text", EMBED_MAX_TOKENS) is None, (
        "CC11: 'text' is the unsupported language this test relies on; pick another"
    )
    content = "".join(f"some prose line number {i} with words in it\n" for i in range(400))
    elapsed = []
    for i in range(5):
        f = root / f"doc{i}.txt"
        f.write_text(content)
        t = time.perf_counter()
        assert chunk_file(f, content, "text", project_root=root), "CC11: no chunks produced"
        elapsed.append(time.perf_counter() - t)
    worst = max(elapsed)
    assert worst < 0.20, (
        f"CC11: {worst:.2f}s to chunk one unsupported-language file — the brute-force "
        f"'auto' detection path is back; a fleet reindex will not finish"
    )


def test_cc12_chunker_built_once_per_language_not_per_file():
    """CC12: one CodeChunker per language, and caching must not alter output.

    Construction re-runs AutoTokenizer, downloaded_languages() and has_language()
    — 19.7ms against 6.3ms to chunk — so building one per file spent 76% of all
    chunking CPU on a chunker it threw away. Output is byte-identical either way,
    so correctness alone cannot see a regression here: the cache counters are the
    discriminator, and they go red the moment chunk_file constructs its own again.
    """
    from pathlib import Path

    from rag_search.index.chunker import _code_chunker, chunk_file
    root = Path(__file__).resolve().parents[2] / "rag_search"
    files = [f for f in sorted(root.rglob("*.py"))[:25] if f.read_text().strip()]
    assert len(files) >= 10, f"CC12: need a corpus, found {len(files)} under {root}"

    fresh = []
    for f in files:
        _code_chunker.cache_clear()  # a construction per file, exactly as before the fix
        fresh.append([c.content for c in chunk_file(f, f.read_text(), "python", project_root=root)])

    _code_chunker.cache_clear()
    cached = [
        [c.content for c in chunk_file(f, f.read_text(), "python", project_root=root)]
        for f in files
    ]
    info = _code_chunker.cache_info()

    assert cached == fresh, "CC12: reusing the chunker changed chunk content"
    assert info.misses == 1, (
        f"CC12: {info.misses} chunkers built for {len(files)} files of one language"
    )
    assert info.hits == len(files) - 1, (
        f"CC12: cache served {info.hits} of {len(files)} files — chunk_file is building "
        f"its own CodeChunker again"
    )


def test_cc10_vector_migration_leaves_the_graph_alone(embedder, standalone_project_path):
    """CC10: re-embedding drifted vectors must not touch graph.db.

    Drift was first wired to _index_project, which opens with gs.clear() — so
    migrating a chunk-size change would have wiped every project's communities and
    forced the whole fleet's LLM narration to be bought again. Chunk shape has no
    bearing on the tree-sitter graph, so the two must stay independent.
    """
    import hashlib

    from rag_search.core.config import project_graph_db, project_vector_db
    from rag_search.daemon.sweeps import _reindex_vectors
    from rag_search.index.store import VectorStore

    gdb = project_graph_db(standalone_project_path)
    assert gdb.exists(), f"CC10: fixture has no graph to protect at {gdb}"
    before = hashlib.sha256(gdb.read_bytes()).hexdigest()

    _reindex_vectors(standalone_project_path)

    assert hashlib.sha256(gdb.read_bytes()).hexdigest() == before, (
        "CC10: vector migration rewrote graph.db — community summaries are collateral"
    )
    vs = VectorStore(project_vector_db(standalone_project_path))
    try:
        assert vs.count() > 0, "CC10: migration left the vector index empty"
        assert vs.stale_signature() is None, (
            "CC10: migrated vectors still report stale — the pass would repeat forever"
        )
    finally:
        vs.close()
