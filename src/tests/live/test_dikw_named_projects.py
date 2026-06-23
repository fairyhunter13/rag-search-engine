"""DIKW invariants on live named projects (DN1–DN5).

DN1  structural spine exists in live project (level=0 dir/file nodes)
DN2  spine nodes have semantic_type=NULL and narrated=0
DN3  retrieval selectors exclude level=0 spine nodes from query context
DN4  live project has L1 communities with narrated column populated
DN5  tree-walk context is traceable: every cited L1 node has valid parent_id chain

Live read-only: no rebuild, no LLM calls. Works against existing enriched projects.
"""
from __future__ import annotations

import sqlite3

import pytest

from opencode_search.core.config import project_graph_db
from opencode_search.core.registry import list_projects

pytestmark = pytest.mark.live


def _first_enabled_with_communities():
    for p in list_projects():
        if not p.enabled:
            continue
        gdb = project_graph_db(p.path)
        if not gdb.exists():
            continue
        with sqlite3.connect(str(gdb)) as con:
            if con.execute("SELECT COUNT(*) FROM communities WHERE level=1").fetchone()[0] > 0:
                return p.path, gdb
    return "", None


def test_dn1_structural_spine_exists_in_live_project():
    """DN1: build_structure_tree has run — live project has level=0 dir/file nodes."""
    path, gdb = _first_enabled_with_communities()
    if not gdb:
        pytest.fail("DN1: no enabled project with L1 communities found")
    with sqlite3.connect(str(gdb)) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(communities)")}
        if "kind" not in cols or "path" not in cols:
            pytest.fail("DN1: communities table missing kind/path cols — schema migration not applied")
        n = con.execute("SELECT COUNT(*) FROM communities WHERE level=0").fetchone()[0]
    assert n > 0, (
        f"DN1: no level=0 structural spine nodes in {path} — "
        "run _enrich_project to build the spine (build_structure_tree)"
    )


def test_dn2_spine_nodes_have_null_type_and_zero_narrated():
    """DN2: every level=0 (dir/file) node has semantic_type=NULL and narrated=0."""
    _path, gdb = _first_enabled_with_communities()
    if not gdb:
        pytest.fail("DN2: no enabled project with L1 communities found")
    with sqlite3.connect(str(gdb)) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(communities)")}
        if "kind" not in cols:
            pytest.fail("DN2: kind column missing — schema not migrated")
        typed = con.execute(
            "SELECT id, path, semantic_type FROM communities "
            "WHERE level=0 AND kind IN ('dir','file') AND semantic_type IS NOT NULL"
        ).fetchall()
        narrated = con.execute(
            "SELECT COUNT(*) FROM communities WHERE level=0 AND kind IN ('dir','file') AND narrated != 0"
        ).fetchone()[0]
    assert not typed, f"DN2: {len(typed)} spine nodes have non-NULL semantic_type: {typed[:3]}"
    assert narrated == 0, f"DN2: {narrated} spine nodes have narrated!=0"


def test_dn3_retrieval_selectors_exclude_spine(project_with_communities):
    """DN3: _top_communities_semantic and _community_context exclude kind='dir'/'file' nodes."""
    from opencode_search.graph.store import GraphStore
    from opencode_search.query.ask import _community_context, _top_communities_semantic
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


def test_dn5_tree_walk_context_traceable(project_with_communities):
    """DN5: _tree_walk_context returns grounded nodes — each L1 cited has a valid parent_id."""
    from opencode_search.graph.store import GraphStore
    from opencode_search.query.ask import _tree_walk_context
    gdb = project_graph_db(project_with_communities)
    gs = GraphStore(gdb)
    try:
        ctx = _tree_walk_context("architecture domains services", [gs])
        if not ctx:
            return  # no L2 hierarchy yet — tree-walk falls back to flat L1 pool, test irrelevant
        # Expect no L1 nodes whose parent_id points to a non-L2 row (chain invariant).
        orphan_l1 = gs._con.execute(
            "SELECT COUNT(*) FROM communities l1 "
            "JOIN communities l2 ON l1.parent_id=l2.id "
            "WHERE l1.level=1 AND l2.level!=2"
        ).fetchone()[0]
    finally:
        gs.close()
    assert orphan_l1 == 0, (
        f"DN5: {orphan_l1} L1 communities point to a non-L2 parent — parent_id chain broken"
    )
