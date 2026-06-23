"""Phase 2.5 — adaptive reasoning-retrieval (RAGRouter-Bench framing).

RR1  global scope produces hierarchy tree-walk header
RR2  architecture scope produces tree-walk header
RR3  feature scope is code-focused (no tree-walk header)
RR4  tree-walk context grounded — every cited community exists in DB
RR5  adaptive MR — architecture query cites >= refs as narrow query
RR6  determinism MR — same query produces byte-identical context
RR7  _tree_walk_context returns '' when no summaries exist
"""
from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.live


def _open_stores(project_with_communities):
    from opencode_search.core.config import project_graph_db
    from opencode_search.daemon.federation import expand_federation
    from opencode_search.graph.store import GraphStore
    paths = [p for p in expand_federation(project_with_communities)
             if project_graph_db(p).exists()]
    return [GraphStore(project_graph_db(p)) for p in paths]


def test_rr1_global_scope_tree_walk_header(project_with_communities):
    """RR1: global scope context contains hierarchy tree-walk header."""
    from opencode_search.query.ask import compose_answer
    stores = _open_stores(project_with_communities)
    try:
        ctx = compose_answer("How does the overall architecture work?", [], stores, scope="global")
        assert "Architecture" in ctx and ("tree-walk" in ctx.lower() or "community" in ctx.lower()), (
            f"global scope must include Architecture + tree-walk; got: {ctx[:200]!r}"
        )
    finally:
        for s in stores: s.close()


def test_rr2_architecture_scope_tree_walk_header(project_with_communities):
    """RR2: architecture scope context contains tree-walk header."""
    from opencode_search.query.ask import compose_answer
    stores = _open_stores(project_with_communities)
    try:
        ctx = compose_answer("What are the main modules?", [], stores, scope="architecture")
        assert "tree-walk" in ctx.lower() or "Architecture" in ctx, (
            f"architecture scope must include tree-walk header; got: {ctx[:200]!r}"
        )
    finally:
        for s in stores: s.close()


def test_rr3_feature_scope_no_tree_walk(project_with_communities):
    """RR3: feature scope does not include tree-walk header."""
    from opencode_search.query.ask import compose_answer
    stores = _open_stores(project_with_communities)
    try:
        ctx = compose_answer("authenticate user token", [], stores, scope="feature")
        assert "hierarchy tree-walk" not in ctx.lower(), (
            "feature scope must not use tree-walk header"
        )
    finally:
        for s in stores: s.close()


def test_rr4_tree_walk_context_grounded(project_with_communities):
    """RR4: every community title cited in tree-walk exists in the DB."""
    from opencode_search.query.ask import _tree_walk_context
    stores = _open_stores(project_with_communities)
    try:
        ctx = _tree_walk_context("architecture overview", stores)
        if not ctx:
            return
        all_titles: set[str] = set()
        for store in stores:
            for r in store._con.execute("SELECT title FROM communities WHERE title IS NOT NULL").fetchall():
                all_titles.add(r[0])
        for c in re.findall(r"\[(?:[^\]]+? → )?([^\]]+)\]", ctx):
            assert c in all_titles, f"RR4: tree-walk cited {c!r} not in DB (hallucination)"
    finally:
        for s in stores: s.close()


def test_rr5_adaptive_mr(project_with_communities):
    """RR5: architecture query yields >= community refs as narrow pinpoint query."""
    from opencode_search.query.ask import _tree_walk_context
    stores = _open_stores(project_with_communities)
    try:
        arch = len(re.findall(r"\[", _tree_walk_context("overall architecture and main domains", stores)))
        code = len(re.findall(r"\[", _tree_walk_context("authenticate", stores, top_k=1)))
        assert arch >= code, f"RR5: architecture ({arch}) should cite >= refs as narrow ({code})"
    finally:
        for s in stores: s.close()


def test_rr6_determinism_mr(project_with_communities):
    """RR6: same query produces byte-identical tree-walk context on two calls."""
    from opencode_search.query.ask import _tree_walk_context
    stores = _open_stores(project_with_communities)
    try:
        q = "what are the main architectural domains?"
        assert _tree_walk_context(q, stores) == _tree_walk_context(q, stores), (
            "RR6: tree-walk context must be deterministic"
        )
    finally:
        for s in stores: s.close()


def test_rr7_empty_fallback_no_summaries(safe_tmp_path):
    """RR7: _tree_walk_context returns '' when no community summaries exist."""
    from opencode_search.graph.extractor import extract_symbols, symbol_id
    from opencode_search.graph.store import GraphStore
    from opencode_search.query.ask import _tree_walk_context
    fpath = safe_tmp_path / "mod.py"
    fpath.write_text("def foo(): pass\n")
    gs = GraphStore(safe_tmp_path / "g.db")
    for s in extract_symbols(fpath, fpath.read_text(), "python"):
        gs.upsert_symbol(symbol_id(str(fpath), s.name, s.start_line),
                         s.name, s.qualified_name, s.kind, str(fpath),
                         s.start_line, s.end_line, s.language)
    gs.commit()
    assert _tree_walk_context("architecture", [gs]) == "", "RR7: expected '' when no summaries"
    gs.close()
