"""Leiden community detection on the symbol call graph."""
from __future__ import annotations

from collections import Counter

from opencode_search.graph.store import GraphStore


def detect_communities(store: GraphStore, *, resolution: float = 1.0) -> dict[str, int]:
    """Run Leiden on call edges; fall back to file-level grouping if no edges.

    Returns {sid: community_id} and commits assignments + community records.
    """
    import igraph as ig
    import leidenalg

    symbols = store.list_symbols()
    if not symbols:
        return {}

    idx: dict[str, int] = {s["sid"]: i for i, s in enumerate(symbols)}
    all_edges = store._con.execute("SELECT caller_sid,callee_sid FROM edges").fetchall()
    edges_ig = [(idx[c], idx[e]) for c, e in all_edges if c in idx and e in idx]

    g = ig.Graph(n=len(symbols), edges=edges_ig, directed=True)
    if g.ecount() == 0:
        files = sorted({s["file"] for s in symbols})
        file_idx = {f: i for i, f in enumerate(files)}
        mapping: dict[str, int] = {s["sid"]: file_idx[s["file"]] for s in symbols}
    else:
        part = leidenalg.find_partition(
            g.as_undirected(),
            leidenalg.ModularityVertexPartition,
            n_iterations=3,
            resolution_parameter=resolution,
        )
        mapping = {symbols[i]["sid"]: part.membership[i] for i in range(len(symbols))}

    for sid, cid in mapping.items():
        store.assign_community(sid, cid)

    counts = Counter(mapping.values())
    for cid, cnt in counts.items():
        store.upsert_community(cid, level=1, title=None,
                               summary="", member_count=cnt)
    store.commit()
    return mapping
