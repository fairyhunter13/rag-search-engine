"""Leiden community detection on the code structure graph.

Uses igraph + leidenalg to cluster densely-connected symbols into communities.
Runs synchronously; call via asyncio.to_thread for async contexts.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opencode_search.graph.storage import EdgeData, GraphStorage

log = logging.getLogger(__name__)


class CommunityDetector:
    """Detect communities in the code graph using the Leiden algorithm."""

    def detect_communities(self, storage: GraphStorage) -> dict[str, int]:
        """Run Leiden algorithm and return {node_id: community_id} mapping.

        Also writes community assignments back to storage.
        Returns the mapping dict for callers that need it.
        """
        import igraph as ig
        import leidenalg

        from .storage import CommunityData

        nodes = storage.all_nodes()
        edges = storage.all_edges()

        if not nodes:
            return {}

        # Build igraph
        node_ids = [n.id for n in nodes]
        id_to_idx: dict[str, int] = {nid: i for i, nid in enumerate(node_ids)}

        # Only use CALLS and IMPORTS edges for community detection
        g_edges = []
        weights = []
        for e in edges:
            if e.kind not in ("CALLS", "IMPORTS"):
                continue
            fi = id_to_idx.get(e.from_id)
            ti = id_to_idx.get(e.to_id)
            if fi is not None and ti is not None:
                g_edges.append((fi, ti))
                weights.append(e.confidence)

        g = ig.Graph(n=len(nodes), edges=g_edges, directed=True)
        g.es["weight"] = weights

        # Leiden requires undirected graph for ModularityVertexPartition
        g_undirected = g.as_undirected(combine_edges="max")

        partition = leidenalg.find_partition(
            g_undirected,
            leidenalg.ModularityVertexPartition,
            n_iterations=-1,  # run until stable
            seed=42,
        )

        # Build mapping and write back
        node_id_to_community: dict[str, int] = {}
        community_members: dict[int, list[str]] = defaultdict(list)

        for community_id, member_indices in enumerate(partition):
            for idx in member_indices:
                nid = node_ids[idx]
                node_id_to_community[nid] = community_id
                community_members[community_id].append(nid)

        # Batch-write all community assignments in a single SQLite transaction
        storage.set_community_batch(node_id_to_community)

        # Write community records with entry points
        for cid, member_ids in community_members.items():
            entry_points = _find_entry_points(member_ids, set(member_ids), edges, storage)
            storage.upsert_community(CommunityData(
                id=cid,
                node_count=len(member_ids),
                key_entry_points=entry_points,
            ))

        log.debug(
            "community detection: %d nodes → %d communities",
            len(nodes),
            len(partition),
        )
        return node_id_to_community


def _find_entry_points(
    member_ids: list[str],
    member_set: set[str],
    all_edges: list[EdgeData],
    storage: GraphStorage,
    top_n: int = 5,
) -> list[str]:
    """Find community entry points: nodes with most inbound edges from outside."""
    inbound: dict[str, int] = defaultdict(int)
    for e in all_edges:
        if e.kind == "CALLS" and e.to_id in member_set and e.from_id not in member_set:
            inbound[e.to_id] += 1

    top_ids = sorted(inbound, key=lambda x: -inbound[x])[:top_n]
    result = []
    for nid in top_ids:
        n = storage.get_node_by_id(nid)
        if n:
            result.append(n.qualified_name)
    return result
