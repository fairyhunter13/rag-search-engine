"""DIKW invariants on live named projects (DN4–DN5).

DN4  live project has L1 communities with narrated column populated
DN5  all L1 communities have no parent_id pointing to a non-L1 row

Live read-only: no rebuild, no LLM calls. Works against existing enriched projects.

DN3 — "the retrieval selector excludes kind='dir'/'file' spine nodes" — was deleted 2026-07-28.
It was measured non-discriminating: dropping the `kind NOT IN ('dir','file')` clause from
`query/ask.py` left it green. A fleet census explains why — across all 160 graph DBs `kind` has
exactly one value, 'community' (8793 rows). Not one 'dir' or 'file' row exists anywhere, because
the DIKW information spine that wrote them left with tier 3, and no writer remains in
`src/rag_search`. A live project cannot exercise the filter, so DN3 could only ever pass or skip,
and the no-skip policy (test_no_code_semantic_regex.py) correctly refuses the second. The
discriminating guard is **test_abstention.py::AB6**, which builds the spine row synthetically and
does go red when the clause is dropped. (`kind` is a constant column — Step 2a purge candidate
alongside `semantic_type` and `narrated`.)
"""
from __future__ import annotations

import sqlite3

import pytest

from rag_search.core.config import project_graph_db

pytestmark = pytest.mark.live


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
