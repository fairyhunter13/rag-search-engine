"""Leiden community detection on the code structure graph.

Uses igraph + leidenalg to cluster densely-connected symbols into communities.
Runs synchronously; call via asyncio.to_thread for async contexts.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opencode_search.graph.storage import GraphStorage

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

        # Build mapping
        node_id_to_community: dict[str, int] = {}
        community_members: dict[int, list[str]] = defaultdict(list)

        for community_id, member_indices in enumerate(partition):
            for idx in member_indices:
                nid = node_ids[idx]
                node_id_to_community[nid] = community_id
                community_members[community_id].append(nid)

        # Compute cross-community inbound counts in ONE pass over all edges
        # (previously called _find_entry_points per community = O(communities × edges))
        id_to_node = {n.id: n for n in nodes}
        inbound: dict[str, int] = defaultdict(int)
        for e in edges:
            if e.kind == "CALLS":
                fc = node_id_to_community.get(e.from_id)
                tc = node_id_to_community.get(e.to_id)
                if fc is not None and tc is not None and fc != tc:
                    inbound[e.to_id] += 1

        # Group top-5 entry points per community
        entry_counts: dict[int, list[tuple[str, int]]] = defaultdict(list)
        for nid, cnt in inbound.items():
            cid = node_id_to_community.get(nid)
            if cid is not None:
                entry_counts[cid].append((nid, cnt))

        # Batch-write all community assignments in a single SQLite transaction
        storage.set_community_batch(node_id_to_community)

        # Batch-write all community records in a single transaction
        community_records = []
        for cid, member_ids in community_members.items():
            top = sorted(entry_counts.get(cid, []), key=lambda x: -x[1])[:5]
            ep = [id_to_node[nid].qualified_name for nid, _ in top if nid in id_to_node]
            community_records.append(CommunityData(
                id=cid,
                node_count=len(member_ids),
                key_entry_points=ep,
            ))
        storage.upsert_communities_batch(community_records)

        log.debug(
            "community detection: %d nodes → %d communities",
            len(nodes),
            len(partition),
        )
        return node_id_to_community


