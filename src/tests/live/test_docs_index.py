"""Phase 3G — docs indexing (GG1–GG4). Requires live GPU embedder.

Rewritten when docgen was deleted. There is no longer a "generated docs" concept: a
``docs/`` tree is ordinary source, discovered by ``iter_files`` and embedded by
``index_project`` like any other directory, and ``scope="docs"`` is purely a language
predicate over ``_TEXT_LANGS``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.live


def _seed_docs(docs: Path) -> None:
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "README.md").write_text("# Project README\n\nThis is prose content.\n")
    (docs / "guide.md").write_text("# Guide\n\nHow to use this component.\n")


def test_gg1_docs_are_discovered_by_default(safe_tmp_path):
    """GG1: a plain docs/ tree is walked by iter_files with no opt-in flag."""
    from rag_search.index.discover import is_ignored_path, iter_files

    root = safe_tmp_path / "proj"
    root.mkdir()
    (root / "code.py").write_text("def hello(): pass\n")
    docs = root / "docs"
    _seed_docs(docs)

    found = {str(p) for p in iter_files(root)}
    assert any("code.py" in p for p in found), "code.py must be discovered"
    assert any("README.md" in p for p in found), "docs/README.md must be discovered"
    assert any("guide.md" in p for p in found), "docs/guide.md must be discovered"

    assert not is_ignored_path(docs / "README.md", root), (
        "docs/README.md must not be ignored — nothing marks docs trees as generated now"
    )


def test_gg2_docs_round_trip(safe_tmp_path, embedder):
    """GG2: index_project embeds docs/ and search(scope=docs) returns them; idempotent."""
    from rag_search.index.indexer import index_project
    from rag_search.index.store import VectorStore
    from rag_search.query.search import search

    root = safe_tmp_path / "proj2"
    root.mkdir()
    (root / "app.py").write_text("def run(): pass\n")
    docs = root / "docs"
    _seed_docs(docs)

    store_path = safe_tmp_path / "v2.db"
    vs = VectorStore(store_path)
    try:
        # index_project returns (files, chunks) — this unpacked it as a bare int until
        # 2026-07-28, which read as a TypeError rather than a failure of anything it tests.
        f1, n1 = index_project(root, embedder, vs, federation_mode=False)
        assert n1 > 0, f"index_project must embed at least one chunk; got {n1}"
        assert f1 >= 3, f"index_project must walk app.py + both docs files; got {f1} file(s)"

        results = search("prose content guide", embedder, vs, scope="docs", top_k=5)
        assert results, "search(scope=docs) returned no results after index_project"
        assert all(
            r.get("language") in {"markdown", "rst", "text", "html", "css"}
            for r in results
        ), f"scope=docs results must be text langs: {[r.get('language') for r in results]}"

        # Idempotent: 2nd call gives same chunk count (clear + reinsert, net 0 new)
        _f2, n2 = index_project(root, embedder, vs, federation_mode=False)
        assert n2 == n1, f"2nd index_project must be idempotent: got {n2} vs {n1}"
        assert vs.count() == n1, "vector store count must be unchanged after 2nd index"
    finally:
        vs.close()


def test_gg3_scope_purity(safe_tmp_path, embedder):
    """GG3: scope=code returns no _TEXT_LANGS chunk; scope=docs returns only _TEXT_LANGS."""
    from rag_search.index.discover import _TEXT_LANGS
    from rag_search.index.indexer import index_project
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

        code_results = search("function run helper", embedder, vs, scope="code", top_k=20)
        docs_results = search("prose content", embedder, vs, scope="docs", top_k=20)

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


def test_gg4_docs_move_the_source_fingerprint(safe_tmp_path):
    """GG4: docs are source now — adding one moves _source_fingerprint.

    This gate used to assert the opposite (a generated docs tree had to stay invisible to
    the drift signal). With the generated-docs guard deleted, docs/ is ordinary source and
    the property worth pinning is that a prose change is *seen* — otherwise it would never
    be re-embedded.
    """
    from rag_search.daemon.sweeps import _fingerprint_cache, _source_fingerprint

    root = safe_tmp_path / "proj4"
    root.mkdir()
    (root / "main.py").write_text("x = 1\n")
    docs = root / "docs"
    _seed_docs(docs)

    sig_before = _source_fingerprint(str(root))
    (docs / "new_page.md").write_text("# New page\n\nAdded after the first fingerprint.\n")
    # A write under docs/ leaves the ROOT dir mtime alone, so the coarse pre-gate would
    # serve the cached sig and this gate would assert nothing. Drop the entry to force
    # the real stat-walk — the property under test is what the walk sees, not the cache.
    _fingerprint_cache.pop(str(root), None)
    sig_after = _source_fingerprint(str(root))

    assert sig_before != sig_after, (
        f"_source_fingerprint must move when a docs file is added: {sig_before!r}"
    )
