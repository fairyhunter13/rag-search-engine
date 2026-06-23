"""Coarse-resolution L2 hierarchy: k-core partition of the L1 community graph → ~√(n_L1) domains."""
from __future__ import annotations

from collections import Counter

from opencode_search.graph.store import GraphStore

_L2_OFFSET = 10_000


def _coarse_partition(g, target: int) -> list:
    """Deterministic k-core coarse partition: raise k threshold until ≤ target components.

    Finds the lowest k such that the induced subgraph on nodes with coreness≥k has
    ≤ target connected components. Nodes excluded from the k-core get their own singleton
    components. Byte-reproducible for the same input graph.
    """
    n = g.vcount()
    coreness = g.coreness()
    best_membership = list(range(n))  # worst case: each node is its own component
    for k in range(0, max(coreness) + 1):
        core_verts = [i for i, c in enumerate(coreness) if c >= k]
        if not core_verts:
            break
        sub = g.induced_subgraph(core_verts)
        comps = sub.connected_components()
        membership = list(range(n))  # non-core nodes stay as singletons
        for comp_id, comp in enumerate(comps):
            for local_i in comp:
                membership[core_verts[local_i]] = n + comp_id
        # Re-number to [0, n_unique)
        remap: dict[int, int] = {}
        for m in membership:
            if m not in remap:
                remap[m] = len(remap)
        membership = [remap[m] for m in membership]
        n_comps = len(set(membership))
        best_membership = membership
        if n_comps <= target:
            return membership
    return best_membership


def build_hierarchy(store: GraphStore) -> int:
    """Build coarse L2 communities (~√n_L1) by partitioning the symbol call graph.

    Returns count of L2 communities created. No-op if <2 L1 communities or no call edges.
    """
    import igraph as ig

    sym_rows = store._con.execute(
        "SELECT sid, community_id FROM symbols WHERE community_id IS NOT NULL"
    ).fetchall()
    if not sym_rows:
        return 0
    n_l1 = len({r[1] for r in sym_rows})
    if n_l1 < 2:
        return 0
    target = max(2, round(n_l1 ** 0.5))

    edge_rows = store._con.execute("SELECT caller_sid, callee_sid FROM edges").fetchall()
    # Only partition symbols that participate in at least one call edge (singletons can't be merged).
    edge_sids = {sid for r in edge_rows for sid in r}
    sym_rows = [(r[0], r[1]) for r in sym_rows if r[0] in edge_sids]
    if not sym_rows:
        return 0
    sids = [r[0] for r in sym_rows]
    idx = {sid: i for i, sid in enumerate(sids)}
    edges_ig = [(idx[r[0]], idx[r[1]]) for r in edge_rows
                if r[0] in idx and r[1] in idx and r[0] != r[1]]
    g = ig.Graph(n=len(sids), edges=edges_ig, directed=False)
    if g.ecount() == 0:
        return 0

    membership = _coarse_partition(g, target)
    sid_to_coarse = {sids[i]: membership[i] for i in range(len(sids))}

    # Parent each L1 community by plurality of its symbols' coarse assignment.
    l1_votes: dict[int, list[int]] = {}
    for sid, l1_cid in sym_rows:
        l1_votes.setdefault(l1_cid, []).append(sid_to_coarse[sid])
    l1_parent = {l1_cid: Counter(votes).most_common(1)[0][0]
                 for l1_cid, votes in l1_votes.items()}

    store._con.execute("DELETE FROM communities WHERE level >= 2")
    child_counts: Counter = Counter(l1_parent.values())
    for coarse_id, cnt in child_counts.items():
        store.upsert_community(_L2_OFFSET + coarse_id, level=2,
                               title=None, summary=None, member_count=cnt)
    for l1_cid, coarse_id in l1_parent.items():
        store.set_community_parent(l1_cid, _L2_OFFSET + coarse_id)
    store.commit()
    return len(child_counts)
