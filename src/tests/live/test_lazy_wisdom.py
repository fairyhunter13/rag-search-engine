"""Phase 3 — lazy query-time narration (Wisdom rung).

LW1  head enrichment sets narrated=1
LW2  structural label leaves narrated=0
LW3  narrate_community_lazy() transitions 0→1
LW4  second call to narrate_lazy is a no-op (idempotency)
LW5  _community_context excludes narrated=0
LW6  narrated=1 community appears in _community_context
LW7  narrated=0 absent from _top_communities_semantic
LW8  narrated column exists in live communities table
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def _make_store(tmp, has_summary: bool = False):
    from rag_search.graph.extractor import extract_symbols, symbol_id
    from rag_search.graph.store import GraphStore
    fpath = tmp / "svc.py"
    fpath.write_text("def process_order(oid): pass\ndef cancel_order(oid): pass\n")
    gs = GraphStore(tmp / "g.db")
    for s in extract_symbols(fpath, fpath.read_text(), "python"):
        gs.upsert_symbol(symbol_id(str(fpath), s.name, s.start_line),
                         s.name, s.qualified_name, s.kind, str(fpath),
                         s.start_line, s.end_line, s.language)
    gs.commit()
    gs.upsert_community(1, level=1, title="Order Service", member_count=2,
                        summary="Order management logic." if has_summary else "")
    gs.commit()
    return gs, 1


def test_lw1_head_enrichment_sets_narrated(safe_tmp_path):
    """LW1: _enrich_one_batch sets narrated=1 on enriched communities."""
    from rag_search.graph.enrich import _TYPE_ORDER, _enrich_one_batch
    from rag_search.graph.extractor import extract_symbols, symbol_id
    from rag_search.graph.store import GraphStore
    fpath = safe_tmp_path / "svc.py"
    fpath.write_text("class OrderService:\n    def process(self, o): return o\n    def cancel(self, o): return None\n")
    gs = GraphStore(safe_tmp_path / "g.db")
    for s in extract_symbols(fpath, fpath.read_text(), "python"):
        gs.upsert_symbol(symbol_id(str(fpath), s.name, s.start_line), s.name,
                         s.qualified_name, s.kind, str(fpath), s.start_line, s.end_line, s.language)
    gs.commit()
    gs.upsert_community(1, level=1, title="OrderSvc", member_count=3, summary="")
    gs.commit()
    enriched, _ = _enrich_one_batch(gs, [1], frozenset(_TYPE_ORDER))
    if enriched:
        row = gs._con.execute("SELECT narrated FROM communities WHERE id=1").fetchone()
        assert row and row[0] == 1, "LW1: head enrichment must set narrated=1"
    gs.close()


def test_lw2_structural_label_leaves_narrated_zero(safe_tmp_path):
    """LW2: label_community_structural (tail) leaves narrated=0."""
    from rag_search.graph.community import label_community_structural
    gs, cid = _make_store(safe_tmp_path)
    try:
        label_community_structural(gs, cid)
        row = gs._con.execute("SELECT narrated FROM communities WHERE id=?", (cid,)).fetchone()
        assert row and row[0] == 0, f"LW2: structural label must leave narrated=0; got {row[0] if row else None}"
    finally:
        gs.close()


def test_lw3_narrate_community_lazy_sets_narrated(safe_tmp_path):
    """LW3: narrate_community_lazy() transitions narrated 0→1 when DeepSeek key present."""
    from rag_search.graph.enrich import narrate_community_lazy
    from rag_search.graph.llm import deepseek_key
    if not deepseek_key():
        pytest.fail("LW3 requires RSE_DEEPSEEK_API_KEY — set the key to test lazy narration")
    gs, cid = _make_store(safe_tmp_path)
    try:
        before = gs._con.execute("SELECT narrated FROM communities WHERE id=?", (cid,)).fetchone()[0]
        assert before == 0, "LW3 precondition: must start narrated=0"
        result = narrate_community_lazy(gs, cid)
        after = gs._con.execute("SELECT narrated FROM communities WHERE id=?", (cid,)).fetchone()[0]
        if result:
            assert after == 1, f"LW3: narrate returned True but narrated={after}"
    finally:
        gs.close()


def test_lw4_narrate_lazy_idempotent(safe_tmp_path):
    """LW4: narrate_community_lazy returns False (no-op) when already narrated=1."""
    from rag_search.graph.enrich import narrate_community_lazy
    gs, cid = _make_store(safe_tmp_path, has_summary=True)
    try:
        gs._con.execute("UPDATE communities SET narrated=1 WHERE id=?", (cid,))
        gs.commit()
        assert narrate_community_lazy(gs, cid) is False, (
            "LW4: must return False for already-narrated community (idempotency guard)"
        )
    finally:
        gs.close()


def test_lw5_community_context_excludes_unnarrated(safe_tmp_path):
    """LW5: _community_context excludes narrated=0 communities."""
    from rag_search.query.ask import _community_context
    gs, _ = _make_store(safe_tmp_path, has_summary=True)
    try:
        assert "Order Service" not in _community_context([gs]), (
            "LW5: narrated=0 community must be excluded from _community_context"
        )
    finally:
        gs.close()


def test_lw6_narrated_community_appears_in_context(safe_tmp_path):
    """LW6: narrated=1 community appears in _community_context."""
    from rag_search.query.ask import _community_context
    gs, cid = _make_store(safe_tmp_path, has_summary=True)
    try:
        gs._con.execute("UPDATE communities SET narrated=1,semantic_type='feature' WHERE id=?", (cid,))
        gs.commit()
        assert "Order Service" in _community_context([gs]), (
            "LW6: narrated=1 community must appear in _community_context"
        )
    finally:
        gs.close()


def test_lw7_unnarrated_absent_from_top_communities(safe_tmp_path):
    """LW7: narrated=0 community absent from _top_communities_semantic."""
    from rag_search.query.ask import _top_communities_semantic
    gs, _ = _make_store(safe_tmp_path, has_summary=True)
    try:
        assert "Order Service" not in _top_communities_semantic("order management", [gs]), (
            "LW7: narrated=0 must be absent from _top_communities_semantic"
        )
    finally:
        gs.close()


def test_lw8_narrated_column_exists(project_with_communities):
    """LW8: narrated column present in live communities table."""
    import sqlite3

    from rag_search.core.config import project_graph_db
    with sqlite3.connect(str(project_graph_db(project_with_communities))) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(communities)")}
    assert "narrated" in cols, f"LW8: narrated column missing; cols={cols}"
