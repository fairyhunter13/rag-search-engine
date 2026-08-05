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


def test_gg4_docs_only_change_still_reaches_the_index_step(safe_tmp_path):
    """GG4: a docs-only on_change must still reach the index step — else prose never re-embeds.

    Asserted at `on_change`, not at a fingerprint helper. Until 2026-08-05 this gate called
    `_source_fingerprint` and asserted a docs write moved it. That was true and proved nothing:
    HR38 had repointed every live gate to the code-only `_code_source_fingerprint` and left
    `_source_fingerprint` with no production caller at all, so GG4 was green against a function
    the daemon never ran.

    The property HR28 actually needs is an *ordering* one, and it lives in `on_change`:
    `_index_files` runs unconditionally, above the code-only sig gate. It has to, because that
    gate filters docs out by construction (`_code_scan` skips anything failing
    `is_code_language`) — so a docs edit reaches the gate as "no drift" every time, and if the
    gate ever moved above the index call, prose would stop being re-embedded with every existing
    test still green. That is the regression this pins.

    FCG1 (`test_idle_stability.py`) is the mirror image and deliberately stubs `_index_files`
    away to assert the *negative* — docs churn must not wake the graph lane. Between them the
    two halves of HR38's docs behaviour are both watched; before this, only the negative was.
    """
    from rag_search.daemon import sweeps

    root = safe_tmp_path / "proj4"
    root.mkdir()
    (root / "main.py").write_text("x = 1\n")
    docs = root / "docs"
    _seed_docs(docs)

    indexed: list[tuple[str, list]] = []
    orig_index, orig_label = sweeps._index_files, sweeps._label_project
    sweeps._index_files = lambda p, f: indexed.append((p, list(f)))  # type: ignore[assignment]
    sweeps._label_project = lambda *a, **kw: None  # type: ignore[assignment]
    for d in (sweeps._last_labelled_sig, sweeps._last_lane_run, sweeps._last_index_fail):
        d.pop(str(root), None)
    try:
        # Baseline first, and it is the whole test. The hoisted-gate regression cannot fire on a
        # project the gate has never seen — `_last_labelled_sig` is unset, so any sig differs and
        # indexing proceeds. It bites on the *second* event for an unchanged code tree, which is
        # exactly what a docs edit is. Without this baseline pass the assertion below passes
        # against the very regression it exists to catch (confirmed by injection, 2026-08-05).
        sweeps.on_change(str(root), [str(root / "main.py")])
        assert sweeps._graph_lane_join(timeout=180.0), "baseline lane pass did not finish"
        assert sweeps._last_labelled_sig.get(str(root)), "baseline must stamp the drift gate"
        indexed.clear()
        sweeps._last_lane_run.pop(str(root), None)  # bypass the lane debounce, not the sig gate

        new_page = docs / "new_page.md"
        new_page.write_text("# New page\n\nAdded after the first index pass.\n")
        sweeps.on_change(str(root), [str(new_page)])
    finally:
        sweeps._graph_lane_join(timeout=180.0)
        sweeps._index_files, sweeps._label_project = orig_index, orig_label
        for d in (sweeps._last_labelled_sig, sweeps._last_lane_run, sweeps._last_index_fail):
            d.pop(str(root), None)

    assert indexed, (
        "a docs-only on_change must still call _index_files — otherwise a prose edit is "
        "never re-embedded. The index step must stay above on_change's code-only sig gate."
    )
    assert str(new_page) in indexed[0][1], (
        f"the docs file must be in the batch handed to _index_files: {indexed[0][1]}"
    )
