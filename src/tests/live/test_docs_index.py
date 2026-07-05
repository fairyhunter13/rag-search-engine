"""Phase 3G — generated docs re-indexing (GG1–GG4). Requires live GPU embedder."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_GEN_FM = (
    "---\ngenerated: true\nsource_sig: abc123\n"
    "hier_version: fg1+lp2\nc4_level: context\n---\n\n"
)


def _seed_docs(docs: Path) -> None:
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "_meta").mkdir()
    (docs / "_meta" / "provenance.json").write_text(json.dumps({}))
    (docs / "README.md").write_text(_GEN_FM + "# Generated README\n\nThis is generated content.\n")
    (docs / "guide.md").write_text(_GEN_FM + "# Guide\n\nHow to use this component.\n")


def test_gg1_include_flag_decoupling(safe_tmp_path):
    """GG1: iter_files(root) excludes generated docs/; iter_files(root, include_generated_docs=True) includes them."""
    from rag_search.index.discover import iter_files

    root = safe_tmp_path / "proj"
    root.mkdir()
    (root / "code.py").write_text("def hello(): pass\n")
    docs = root / "docs"
    _seed_docs(docs)

    default_files = {str(p) for p in iter_files(root)}
    assert any("code.py" in p for p in default_files), "code.py must be discovered"
    assert not any("docs" in p for p in default_files), "docs/ must be excluded by default"

    include_files = {str(p) for p in iter_files(root, include_generated_docs=True)}
    assert any("README.md" in p for p in include_files), "README.md must be included when flag is True"
    assert any("code.py" in p for p in include_files), "code.py must still be included"


def test_gg2_index_docs_round_trip(safe_tmp_path, embedder):
    """GG2: write docs → index_docs → search(scope=docs) returns a hit; idempotent on 2nd call."""
    from rag_search.index.indexer import index_docs
    from rag_search.index.store import VectorStore
    from rag_search.query.search import search

    root = safe_tmp_path / "proj2"
    root.mkdir()
    docs = root / "docs"
    _seed_docs(docs)

    store_path = safe_tmp_path / "v2.db"
    vs = VectorStore(store_path)
    try:
        n1 = index_docs(str(root), embedder, vs)
        assert n1 > 0, f"index_docs must embed at least one chunk; got {n1}"

        results = search("generated content guide", embedder, vs, scope="docs", top_k=5)
        assert results, "search(scope=docs) returned no results after index_docs"
        assert all(
            r.get("language") in {"markdown", "rst", "text", "html", "css"}
            for r in results
        ), f"scope=docs results must be text langs: {[r.get('language') for r in results]}"

        # Idempotent: 2nd call gives same chunk count (delete + reinsert, net 0 new)
        n2 = index_docs(str(root), embedder, vs)
        assert n2 == n1, f"2nd index_docs must be idempotent: got {n2} vs {n1}"
        assert vs.count() == n1, "vector store count must be unchanged after 2nd index_docs"
    finally:
        vs.close()


def test_gg3_scope_purity(safe_tmp_path, embedder):
    """GG3: scope=code returns no _TEXT_LANGS chunk; scope=docs returns only _TEXT_LANGS."""
    from rag_search.index.discover import _TEXT_LANGS
    from rag_search.index.indexer import index_docs, index_project
    from rag_search.index.store import VectorStore
    from rag_search.query.search import search

    root = safe_tmp_path / "proj3"
    root.mkdir()
    (root / "app.py").write_text("def run(): pass\n")
    (root / "util.go").write_text("package main\nfunc helper() {}\n")
    docs = root / "docs"
    _seed_docs(docs)

    store_path = safe_tmp_path / "v3.db"
    vs = VectorStore(store_path)
    try:
        index_project(root, embedder, vs, federation_mode=False)
        index_docs(str(root), embedder, vs)

        code_results = search("function run helper", embedder, vs, scope="code", top_k=20)
        docs_results = search("generated content", embedder, vs, scope="docs", top_k=20)

        assert code_results, "scope=code returned no results"
        for r in code_results:
            assert r.get("language") not in _TEXT_LANGS, (
                f"scope=code result has text lang {r.get('language')}: {r.get('path')}"
            )

        assert docs_results, "scope=docs returned no results"
        for r in docs_results:
            assert r.get("language") in _TEXT_LANGS, (
                f"scope=docs result has non-text lang {r.get('language')}: {r.get('path')}"
            )
    finally:
        vs.close()


def test_gg4_churn_guard(safe_tmp_path, embedder):
    """GG4: index_docs doesn't change _source_fingerprint; is_ignored_path(docs/x.md) stays True."""
    from rag_search.daemon.sweeps import _source_fingerprint
    from rag_search.index.discover import is_ignored_path
    from rag_search.index.indexer import index_docs
    from rag_search.index.store import VectorStore

    root = safe_tmp_path / "proj4"
    root.mkdir()
    (root / "main.py").write_text("x = 1\n")
    docs = root / "docs"
    _seed_docs(docs)

    sig_before = _source_fingerprint(str(root))

    store_path = safe_tmp_path / "v4.db"
    vs = VectorStore(store_path)
    try:
        index_docs(str(root), embedder, vs)
    finally:
        vs.close()

    sig_after = _source_fingerprint(str(root))
    assert sig_before == sig_after, (
        f"_source_fingerprint changed after index_docs: {sig_before!r} → {sig_after!r}"
    )

    readme = docs / "README.md"
    assert is_ignored_path(readme, root), "is_ignored_path must still be True for docs/README.md"
