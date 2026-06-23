"""Coarse-resolution L2 hierarchy: partition the L1 community call graph → ~√(n_L1) domains."""
from __future__ import annotations

import os
from collections import Counter

from opencode_search.graph.store import GraphStore

# Bump when the L2 partitioning algorithm changes so reconcile re-derives stale hierarchies.
HIER_VERSION = "lp2"  # two-phase L1-community-graph partition (fastgreedy + capped dir-group), v2

_L2_OFFSET = 10_000


def build_hierarchy(store: GraphStore) -> int:
    """Build coarse L2 communities (~√n_L1) by partitioning the L1 community call graph.

    Phase 1: fastgreedy on connected L1 communities → ≤target groups.
    Phase 2: isolated L1 communities (no cross-community edges) → group by top-level directory.
    Edge-sparse repos with no cross-community edges at all fall through to Phase 2 only,
    producing a directory-based L2 instead of an empty hierarchy.
    Returns count of L2 communities created. No-op if <2 L1 communities.
    """
    import igraph as ig

    sym_rows = store._con.execute(
        "SELECT sid, community_id, file FROM symbols WHERE community_id IS NOT NULL"
    ).fetchall()
    if not sym_rows:
        return 0
    l1_ids = sorted({r[1] for r in sym_rows})
    n_l1 = len(l1_ids)
    if n_l1 < 2:
        return 0
    target = max(2, round(n_l1 ** 0.5))

    sid_to_l1 = {r[0]: r[1] for r in sym_rows}
    l1_idx = {cid: i for i, cid in enumerate(l1_ids)}

    # Build L1-community graph: one node per L1 community, edge for each cross-community call.
    edge_rows = store._con.execute("SELECT caller_sid, callee_sid FROM edges").fetchall()
    comm_edges: set[tuple[int, int]] = set()
    for caller_sid, callee_sid in edge_rows:
        la = sid_to_l1.get(caller_sid)
        lb = sid_to_l1.get(callee_sid)
        if la is not None and lb is not None and la != lb:
            a, b = l1_idx[la], l1_idx[lb]
            comm_edges.add((min(a, b), max(a, b)))

    # Top-level directory mapping (needed for both Phase 2 and edge-sparse fallthrough).
    all_files = [r[2] for r in sym_rows if r[2]]
    root_prefix = (os.path.commonpath(all_files) + os.sep) if len(all_files) > 1 else ""
    l1_to_topdir: dict[int, str] = {}
    for _sid, l1_cid, file in sym_rows:
        vi = l1_idx[l1_cid]
        if vi not in l1_to_topdir and file:
            rel = file[len(root_prefix):] if root_prefix and file.startswith(root_prefix) else file
            l1_to_topdir[vi] = rel.split(os.sep)[0] or "__root__"

    l2_label: dict[int, int] = {}
    n_fg = 0

    if comm_edges:
        conn_verts = sorted({v for e in comm_edges for v in e})
        iso_verts = sorted(set(range(n_l1)) - set(conn_verts))

        # Phase 1: fastgreedy on the connected subgraph → ≤target groups labeled 0..n_fg-1.
        g_full = ig.Graph(n=n_l1, edges=list(comm_edges), directed=False)
        g_conn = g_full.induced_subgraph(conn_verts)
        n_cut = min(target, len(conn_verts))
        mship = g_conn.community_fastgreedy().as_clustering(n=n_cut).membership
        l2_label = {conn_verts[i]: mship[i] for i in range(len(conn_verts))}
        n_fg = max(mship) + 1 if mship else 0
    else:
        # Edge-sparse repo: no cross-community edges → all L1 communities are isolated.
        iso_verts = list(range(n_l1))

    # Phase 2: isolated L1 communities → group by top-level directory, capped so that
    # total L2 ≤ target (Phase-1 used n_fg slots; Phase-2 gets remaining = target - n_fg).
    # When directories exceed the remaining capacity, overflow dirs merge into "__other__".
    remaining = max(1, target - n_fg)
    iso_dir_set = {l1_to_topdir.get(vi, "__no_file__") for vi in iso_verts}
    if len(iso_dir_set) <= remaining:
        top_dirs: set[str] = iso_dir_set
    else:
        freq: Counter = Counter(l1_to_topdir.get(vi, "__no_file__") for vi in iso_verts)
        top_dirs = {d for d, _ in freq.most_common(remaining - 1)}
    dir_to_gid: dict[str, int] = {}
    for vi in iso_verts:
        d = l1_to_topdir.get(vi, "__no_file__")
        bucket = d if d in top_dirs else "__other__"
        if bucket not in dir_to_gid:
            dir_to_gid[bucket] = n_fg + len(dir_to_gid)
        l2_label[vi] = dir_to_gid[bucket]

    l1_parent = {l1_ids[vi]: l2_label[vi] for vi in range(n_l1)}

    store._con.execute("DELETE FROM communities WHERE level >= 2")
    child_counts: Counter = Counter(l1_parent.values())
    for coarse_id, cnt in child_counts.items():
        store.upsert_community(_L2_OFFSET + coarse_id, level=2,
                               title=None, summary=None, member_count=cnt)
    for l1_cid, coarse_id in l1_parent.items():
        store.set_community_parent(l1_cid, _L2_OFFSET + coarse_id)
    store.commit()
    return len(child_counts)
