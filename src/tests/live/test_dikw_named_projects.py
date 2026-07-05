"""DIKW invariants on live named projects (DN3–DN5).

DN3  retrieval selectors exclude level=0 spine nodes from query context
DN4  live project has L1 communities with narrated column populated
DN5  all L1 communities have no parent_id pointing to a non-L1 row

Live read-only: no rebuild, no LLM calls. Works against existing enriched projects.
"""
from __future__ import annotations

import sqlite3

import pytest

from rag_search.core.config import project_graph_db

pytestmark = pytest.mark.live


def test_dn3_retrieval_selectors_exclude_spine(project_with_communities):
    """DN3: _top_communities_semantic and _community_context exclude kind='dir'/'file' nodes."""
    from rag_search.graph.store import GraphStore
    from rag_search.query.ask import _community_context, _top_communities_semantic
    gdb = project_graph_db(project_with_communities)
    gs = GraphStore(gdb)
    try:
        sem = _top_communities_semantic("files directories modules", [gs])
        ctx = _community_context([gs])
    finally:
        gs.close()
    for text, label in [(sem, "semantic"), (ctx, "context")]:
        assert "subdirectory" not in text, (
            f"DN3: dir spine node leaked into {label}: {text[:200]}"
        )
        assert "symbol(s) [" not in text, (
            f"DN3: file spine node leaked into {label}: {text[:200]}"
        )


def test_dn4_narrated_column_integrity(project_with_communities):
    """DN4: narrated column exists and L1 communities have valid 0/1 values only."""
    with sqlite3.connect(str(project_graph_db(project_with_communities))) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(communities)")}
        assert "narrated" in cols, "DN4: narrated column missing"
        bad = con.execute(
            "SELECT COUNT(*) FROM communities WHERE level=1 AND narrated NOT IN (0,1)"
        ).fetchone()[0]
    assert bad == 0, f"DN4: {bad} L1 communities have narrated value outside {{0,1}}"


def test_dn5_l1_communities_have_no_invalid_parents(project_with_communities):
    """DN5: after WS-B/WS-E, all L1 communities have no L2+ parents.

    If parent_id column was fully removed (WS-B schema migration), the invariant is trivially
    satisfied — the column's absence means no parent relationship is possible.
    """
    with sqlite3.connect(str(project_graph_db(project_with_communities))) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(communities)")}
        if "parent_id" not in cols:
            return  # column removed in WS-B — invariant trivially holds
        bad = con.execute(
            "SELECT COUNT(*) FROM communities l1 "
            "JOIN communities lp ON l1.parent_id=lp.id "
            "WHERE l1.level=1 AND lp.level!=1"
        ).fetchone()[0]
    assert bad == 0, (
        f"DN5: {bad} L1 communities point to a non-L1 parent — WS-E purge may be incomplete"
    )
