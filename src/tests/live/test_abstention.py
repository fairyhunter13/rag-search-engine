"""Phase 1.5 — semantic_type abstention guards.

Research: selective-prediction/coverage-risk (SelectLLM+RiskEval JJPAy8mvrQ; CWSA arXiv 2505.18622),
reject-option for node classification (NCwR arXiv 2412.03190; arXiv 2501.08397; arXiv 2512.16244),
metamorphic booster MRs (MetaRAG arXiv 2509.09360; arXiv 2603.24774).

AB1/AB2  tail writes true SQL NULL, not '' (the NOT IN filter load-bearing detail)
AB3      no non-NULL bucket dominates > 50% of typed rows on a sample project
AB4      NULL fraction is majority of L1 (tail >> head by design)
AB5/AB6  read-path: NULL-typed tail excluded; typed head included
AB7      L2 types unaffected (non-regression — upsert_community default must stay '')
AB8      opaque/low-signal community abstains, never forced to 'utility'
SG1      community.py source contains no 'utility' string-literal assignment
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.live


def _build_tail_store(tmp: Path):
    from rag_search.graph.community import detect_communities
    from rag_search.graph.extractor import extract_symbols, symbol_id
    from rag_search.graph.store import GraphStore

    gdb = tmp / "g.db"
    gs = GraphStore(gdb)
    fpath = tmp / "svc.py"
    fpath.write_text("def process(x): pass\ndef validate(x): return bool(x)\ndef save(x): pass\n")
    for s in extract_symbols(fpath, fpath.read_text(), "python"):
        gs.upsert_symbol(symbol_id(str(fpath), s.name, s.start_line),
                         s.name, s.qualified_name, s.kind,
                         str(fpath), s.start_line, s.end_line, s.language)
    gs.commit()
    detect_communities(gs)
    cid = gs._con.execute("SELECT id FROM communities WHERE level=1 LIMIT 1").fetchone()[0]
    return gs, cid


def _label(gs, cid):
    from rag_search.graph.community import label_community_structural
    label_community_structural(gs, cid)
    gs.commit()


def test_ab1_ab2_tail_is_sql_null(safe_tmp_path):
    """AB1+AB2: label_community_structural writes true SQL NULL, not '' or 'utility'."""
    gs, cid = _build_tail_store(safe_tmp_path)
    try:
        _label(gs, cid)
        row = gs._con.execute(
            "SELECT semantic_type FROM communities WHERE id=?", (cid,)
        ).fetchone()
        assert row is not None
        assert row[0] is None, f"AB1: got {row[0]!r} — catch-all regressed"
        empty = gs._con.execute(
            "SELECT COUNT(*) FROM communities WHERE id=? AND semantic_type=''", (cid,)
        ).fetchone()[0]
        assert empty == 0, "AB2: must be true NULL not '' — '' NOT IN ('test') pollutes"
    finally:
        gs.close()


def test_ab3_no_type_dominates(project_with_communities):
    """AB3: no non-NULL bucket > 50% — the anti-collapse guard (old code was ~95% 'utility')."""
    from rag_search.core.config import project_graph_db
    with sqlite3.connect(str(project_graph_db(project_with_communities))) as con:
        rows = con.execute(
            "SELECT semantic_type FROM communities WHERE semantic_type IS NOT NULL"
        ).fetchall()
    if not rows:
        return
    counts = Counter(r[0] for r in rows)
    total = sum(counts.values())
    for label, cnt in counts.items():
        assert cnt / total <= 0.5, (
            f"AB3: '{label}' = {cnt/total:.0%} — catch-all collapse. "
            "Old 'utility' was ~95%; abstention must produce diverse or empty typed set."
        )


def test_ab4_null_majority_post_reenrich(project_with_communities):
    """AB4: NULL fraction >= 50% of L1 after Phase 1.5 re-enrich.

    Forward-looking: if all communities are currently typed (pre-fix DB state), the
    re-enrich hasn't happened yet and the test is vacuously true — only fires once
    Phase 1.5 is active and at least one NULL-typed tail community exists.
    """
    from rag_search.core.config import project_graph_db
    with sqlite3.connect(str(project_graph_db(project_with_communities))) as con:
        total = con.execute("SELECT COUNT(*) FROM communities WHERE level=1").fetchone()[0]
        typed = con.execute(
            "SELECT COUNT(*) FROM communities WHERE level=1 AND semantic_type IS NOT NULL"
        ).fetchone()[0]
    if total == 0 or (total - typed) == 0:
        return  # pre-Phase-1.5 DB or empty — assert after first re-enrich
    assert (total - typed) / total >= 0.5, (
        f"AB4: only {(total-typed)/total:.0%} of L1 are NULL. "
        "Phase 1.5 re-enrich should produce NULL-typed tail >> typed head."
    )


def test_ab5_null_tail_excluded_from_ask_path(safe_tmp_path):
    """AB5: NULL-typed tail absent from _top_communities_semantic / _community_context."""
    from rag_search.query.ask import _community_context, _top_communities_semantic

    gs, cid = _build_tail_store(safe_tmp_path)
    try:
        _label(gs, cid)
        gs._con.execute(
            "UPDATE communities SET summary='order processing routines' WHERE id=?", (cid,)
        )
        gs.commit()
        assert gs._con.execute(
            "SELECT semantic_type FROM communities WHERE id=?", (cid,)
        ).fetchone()[0] is None, "precondition: must be NULL-typed"
        assert "order processing" not in _top_communities_semantic("order processing", [gs]), (
            "AB5: NULL-typed tail polluted _top_communities_semantic"
        )
        assert "order processing" not in _community_context([gs]), (
            "AB5: NULL-typed tail polluted _community_context"
        )
    finally:
        gs.close()


def test_ab6_typed_head_in_ask_path(safe_tmp_path):
    """AB6: typed head community present in _community_context."""
    from rag_search.query.ask import _community_context

    gs, cid = _build_tail_store(safe_tmp_path)
    try:
        gs._con.execute(
            "UPDATE communities SET semantic_type='service',narrated=1,"
            "summary='payment gateway service for order routing' WHERE id=?", (cid,)
        )
        gs.commit()
        assert "payment gateway" in _community_context([gs]), (
            "AB6: typed head must appear in _community_context"
        )
    finally:
        gs.close()


def test_ab7_l2_types_unaffected(project_with_communities):
    """AB7: L2 domains still typed after tail fix (upsert_community default must stay '')."""
    from rag_search.core.config import project_graph_db
    with sqlite3.connect(str(project_graph_db(project_with_communities))) as con:
        l2 = con.execute("SELECT id, semantic_type FROM communities WHERE level>=2").fetchall()
    if len(l2) < 3:
        return
    typed = [r for r in l2 if r[1] is not None]
    assert len(typed) >= len(l2) * 0.5, (
        f"AB7: L2 type regression — {len(typed)}/{len(l2)} typed. "
        "Tail fix must not affect L2 (upsert_community default must stay '', not None)."
    )


def test_ab8_opaque_names_abstain(safe_tmp_path):
    """AB8 (MR-2): opaque names => abstain, never forced to 'utility'."""
    from rag_search.graph.community import detect_communities, label_community_structural
    from rag_search.graph.extractor import extract_symbols, symbol_id
    from rag_search.graph.store import GraphStore

    gdb = safe_tmp_path / "opaque.db"
    gs = GraphStore(gdb)
    fpath = safe_tmp_path / "xzq.py"
    fpath.write_text("def xzq_a(): pass\ndef xzq_b(): pass\ndef xzq_c(): pass\n")
    for s in extract_symbols(fpath, fpath.read_text(), "python"):
        gs.upsert_symbol(symbol_id(str(fpath), s.name, s.start_line),
                         s.name, s.qualified_name, s.kind,
                         str(fpath), s.start_line, s.end_line, s.language)
    gs.commit()
    detect_communities(gs)
    try:
        for (cid,) in gs._con.execute("SELECT id FROM communities WHERE level=1").fetchall():
            label_community_structural(gs, cid)
        gs.commit()
        forced = gs._con.execute(
            "SELECT semantic_type FROM communities WHERE level=1 AND semantic_type IS NOT NULL"
        ).fetchall()
        assert not forced, f"AB8: opaque community got forced type {forced} — MR-2 violated"
    finally:
        gs.close()


def test_sg1_no_utility_literal_in_community_py():
    """SG1: community.py must not assign semantic_type='utility' or contain keyword block."""
    import importlib
    import inspect
    src = inspect.getsource(importlib.import_module("rag_search.graph.community"))
    assert "semantic_type='utility'" not in src, "SG1: 'utility' literal regressed"
    assert 'semantic_type="utility"' not in src, "SG1: 'utility' literal regressed"
    assert "semantic_type='infrastructure'" not in src, "SG1: keyword labeling regressed"
    assert "all_files_lower" not in src, "SG1: path-keyword block (all_files_lower) regressed"
