"""Partition-quality metrics for the L1 community detection (zero LLM, deterministic).

Based on arXiv 2501.07025 (quality-without-ground-truth) with the caveat that
modularity Q alone is unreliable (exponentially-many near-optimal partitions on sparse
graphs); we gate on a COMPOSITE signal: singleton_ratio + coverage + Q.
"""
from __future__ import annotations

from opencode_search.graph.store import GraphStore


def partition_quality(store: GraphStore) -> dict:
    """Compute partition-quality metrics from the L1 community assignment.

    Returns a dict with: n_l1, n_l2, level_max, modularity_q, coverage,
    singleton_ratio, degenerate.  Always cheap (SQL + igraph, zero LLM calls).
    """
    import igraph as ig

    rows = store._con.execute(
        "SELECT sid, community_id FROM symbols WHERE community_id IS NOT NULL"
    ).fetchall()
    if not rows:
        return {
            "n_l1": 0, "n_l2": 0, "level_max": 0,
            "modularity_q": 0.0, "coverage": 0.0,
            "singleton_ratio": 0.0, "degenerate": False,
        }

    sids = [r[0] for r in rows]
    raw_cids = [r[1] for r in rows]
    idx: dict[str, int] = {sid: i for i, sid in enumerate(sids)}

    # Remap community IDs to contiguous integers for igraph.modularity()
    unique_cids = sorted(set(raw_cids))
    cid_map = {cid: i for i, cid in enumerate(unique_cids)}
    membership = [cid_map[cid] for cid in raw_cids]

    edge_rows = store._con.execute("SELECT caller_sid, callee_sid FROM edges").fetchall()
    edges_ig = [(idx[c], idx[e]) for c, e in edge_rows if c in idx and e in idx]
    g = ig.Graph(n=len(sids), edges=edges_ig, directed=True)
    ec = g.ecount()

    modularity_q = g.as_undirected().modularity(membership) if ec > 0 else 0.0

    if ec > 0:
        intra = sum(
            1 for c_sid, e_sid in edge_rows
            if c_sid in idx and e_sid in idx
            and membership[idx[c_sid]] == membership[idx[e_sid]]
        )
        coverage = intra / ec
    else:
        coverage = 0.0

    l1_total = store._con.execute(
        "SELECT COUNT(*) FROM communities WHERE level=1"
    ).fetchone()[0]
    l1_singleton = store._con.execute(
        "SELECT COUNT(*) FROM communities WHERE level=1 AND member_count=1"
    ).fetchone()[0]
    singleton_ratio = (l1_singleton / l1_total) if l1_total > 0 else 0.0

    n_l1 = len(unique_cids)
    n_l2 = store._con.execute(
        "SELECT COUNT(*) FROM communities WHERE level=2"
    ).fetchone()[0]
    level_max_row = store._con.execute(
        "SELECT MAX(level) FROM communities"
    ).fetchone()[0]
    level_max = level_max_row if level_max_row else 1

    # Composite degenerate verdict (no edge-free penalty for coverage/modularity).
    degenerate = bool(
        singleton_ratio >= 0.60
        or (ec > 0 and coverage < 0.20)
        or (ec > 0 and n_l1 >= 2 and modularity_q < 0.05)
    )

    return {
        "n_l1": n_l1, "n_l2": n_l2, "level_max": level_max,
        "modularity_q": round(modularity_q, 4),
        "coverage": round(coverage, 4),
        "singleton_ratio": round(singleton_ratio, 4),
        "degenerate": degenerate,
    }
