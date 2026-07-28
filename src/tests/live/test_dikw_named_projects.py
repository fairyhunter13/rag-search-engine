"""DIKW invariants on live named projects (DN5).

DN5  all L1 communities have no parent_id pointing to a non-L1 row

Live read-only: no rebuild, no LLM calls. Works against existing enriched projects.

DN3 and DN4 were both deleted, and the same census killed them. DN3 — "the retrieval selector
excludes kind='dir'/'file' spine nodes" — went on 2026-07-28 after it was measured
non-discriminating: dropping the `kind NOT IN ('dir','file')` clause from `query/ask.py` left it
green, because across all 160 graph DBs `kind` had exactly one value, 'community' (8793 rows).
The DIKW information spine that wrote 'dir'/'file' rows left with tier 3. DN4 — "the narrated
column exists and holds only 0/1" — went with the R2 purge that acted on that census: `narrated`,
`semantic_type`, `kind` and `path` are all dropped from the schema now, so DN4's first assertion
is false by design and its second has no column to range over. A guard whose subject was deleted
does not get re-pointed; it gets removed.
"""
from __future__ import annotations

import sqlite3

import pytest

from rag_search.core.config import project_graph_db

pytestmark = pytest.mark.live


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
