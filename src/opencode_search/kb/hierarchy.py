"""Recursive Leiden hierarchy: L2+ community-of-communities."""
from __future__ import annotations

from collections import Counter

from opencode_search.graph.store import GraphStore

_L2_OFFSET = 10_000


def build_hierarchy(store: GraphStore, *, resolution: float = 1.0) -> int:
    """Build L2 meta-communities from cross-L1-community edges.

    Returns count of L2 communities created. No-op if <2 L1 communities or no edges.
    """
    import igraph as ig
    import leidenalg

    l1_rows = store._con.execute(
        "SELECT DISTINCT community_id FROM symbols WHERE community_id IS NOT NULL"
    ).fetchall()
    l1_ids = [r[0] for r in l1_rows]
    if len(l1_ids) < 2:
        return 0

    cross_edges = store._con.execute(
        """SELECT s1.community_id, s2.community_id, COUNT(*) as w
           FROM edges e
           JOIN symbols s1 ON e.caller_sid=s1.sid
           JOIN symbols s2 ON e.callee_sid=s2.sid
           WHERE s1.community_id IS NOT NULL AND s2.community_id IS NOT NULL
             AND s1.community_id != s2.community_id
           GROUP BY s1.community_id, s2.community_id"""
    ).fetchall()
    if not cross_edges:
        return 0

    idx = {cid: i for i, cid in enumerate(l1_ids)}
    edges_ig = [(idx[r[0]], idx[r[1]]) for r in cross_edges if r[0] in idx and r[1] in idx]
    weights = [float(r[2]) for r in cross_edges if r[0] in idx and r[1] in idx]

    g = ig.Graph(n=len(l1_ids), edges=edges_ig, directed=False)
    if g.ecount() == 0:
        return 0

    part = leidenalg.find_partition(
        g, leidenalg.ModularityVertexPartition,
        weights=weights, n_iterations=3,
    )
    l2_mapping = {l1_ids[i]: part.membership[i] for i in range(len(l1_ids))}
    counts = Counter(l2_mapping.values())
    for l2_id, cnt in counts.items():
        store.upsert_community(_L2_OFFSET + l2_id, level=2,
                               title=f"Domain {l2_id}", summary="", member_count=cnt)
    store.commit()
    return len(counts)
