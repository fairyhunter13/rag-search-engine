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

        # Only assign community_id for nodes in real clusters (size >= 2).
        # Singleton nodes keep community_id=NULL — they don't benefit from
        # clustering and storing 100k+ singleton community records wastes space.
        real_assignments: dict[str, int] = {
            nid: cid
            for cid, member_ids in community_members.items()
            if len(member_ids) >= 2
            for nid in member_ids
        }
        # NULL out community_id for nodes that are now singletons (or were previously
        # in a community that is no longer a real cluster).
        storage.set_community_batch_with_null(node_id_to_community, real_assignments)

        # Batch-write only real community records (node_count >= 2)
        community_records = []
        for cid, member_ids in community_members.items():
            if len(member_ids) < 2:
                continue  # skip singletons — saves storage, not useful for enrichment
            top = sorted(entry_counts.get(cid, []), key=lambda x: -x[1])[:5]
            ep = [id_to_node[nid].qualified_name for nid, _ in top if nid in id_to_node]
            community_records.append(CommunityData(
                id=cid,
                node_count=len(member_ids),
                key_entry_points=ep,
            ))
        storage.upsert_communities_batch(community_records)

        n_real = len(community_records)
        n_singletons = len(community_members) - n_real
        log.debug(
            "community detection: %d nodes → %d communities (%d singletons skipped)",
            len(nodes), n_real, n_singletons,
        )
        return real_assignments

    def build_hierarchy(
        self,
        storage: GraphStorage,
        max_levels: int = 10,
        max_communities: int = 2000,
    ) -> int:
        """Build a recursive Leiden hierarchy on top of the level-1 communities.

        Starting from the existing level-1 communities, iteratively constructs
        higher-level meta-communities by treating each community as a node and
        inter-community CALLS edges as edges between those nodes.

        Stops when:
        - The meta-graph has fewer than 5 nodes (too small to partition further)
        - The resulting partition has only 1 community (fully connected)
        - max_levels is reached

        Args:
            max_communities: Cap on level-1 communities used. For large projects
                (e.g. >2000 communities) the meta-graph becomes impractical to run
                Leiden on — cap to the largest communities (most connected/important).

        Returns the number of additional levels built (0 if none were possible).
        """
        import igraph as ig
        import leidenalg

        from .storage import CommunityData

        # Load the original node→community mapping from the DB
        edges = storage.all_edges()

        # Get level-1 communities — sort by node_count desc, cap at max_communities
        level1_all = storage.get_communities(level=1)
        if len(level1_all) < 5:
            log.debug("hierarchy: only %d level-1 communities, skipping hierarchy", len(level1_all))
            return 0
        level1 = sorted(level1_all, key=lambda c: c.node_count, reverse=True)[:max_communities]
        if len(level1_all) > max_communities:
            log.info(
                "hierarchy: capping to top-%d communities (project has %d total)",
                max_communities, len(level1_all),
            )

        # Wipe any stale L2+ communities so this call is idempotent (re-runnable).
        # clear_hierarchy() NULLs L1 parent_community_id before deleting parents to
        # satisfy PRAGMA foreign_keys=ON — avoids FOREIGN KEY constraint failed.
        storage.clear_hierarchy()

        # current_level_membership: {community_id (at current_level) → parent_id (level+1)}
        # Start with level-1 community IDs as "nodes" in the meta-graph
        current_communities = {c.id: c for c in level1}
        levels_built = 0

        # Build a mapping from node_id → community_id (level 1)
        # We need the nodes table for this
        all_nodes = storage.all_nodes()
        node_to_community: dict[str, int] = {}
        for n in all_nodes:
            if n.community_id is not None:
                node_to_community[n.id] = n.community_id

        for current_level in range(1, max_levels):
            next_level = current_level + 1

            # Build meta-graph: nodes = current-level community IDs,
            # edges = cross-community CALLS (weighted by count)
            comm_ids = sorted(current_communities.keys())
            if len(comm_ids) < 5:
                break

            comm_to_idx = {cid: i for i, cid in enumerate(comm_ids)}

            # Count cross-community edges
            edge_weights: dict[tuple[int, int], float] = defaultdict(float)
            for e in edges:
                if e.kind not in ("CALLS", "IMPORTS"):
                    continue
                fc = node_to_community.get(e.from_id)
                tc = node_to_community.get(e.to_id)
                if fc is None or tc is None or fc == tc:
                    continue
                fi = comm_to_idx.get(fc)
                ti = comm_to_idx.get(tc)
                if fi is None or ti is None:
                    continue
                key = (min(fi, ti), max(fi, ti))
                edge_weights[key] += e.confidence

            if not edge_weights:
                log.debug("hierarchy level %d: no cross-community edges, stopping", next_level)
                break

            g_edges = list(edge_weights.keys())
            g_weights = [edge_weights[k] for k in g_edges]
            meta_g = ig.Graph(n=len(comm_ids), edges=g_edges, directed=False)
            meta_g.es["weight"] = g_weights

            partition = leidenalg.find_partition(
                meta_g,
                leidenalg.ModularityVertexPartition,
                n_iterations=-1,
                seed=42,
                weights="weight",
            )

            if len(partition) <= 1:
                log.debug("hierarchy level %d: single partition, stopping", next_level)
                break

            # Build new community data for this level
            new_communities: dict[int, CommunityData] = {}
            # Assign IDs: use offset to avoid collision with level-1 IDs
            # Use negative or large offset: level_N_id = level * 10_000_000 + partition_idx
            id_offset = current_level * 10_000_000
            child_to_parent: dict[int, int] = {}  # child community_id → parent community_id

            for partition_idx, member_indices in enumerate(partition):
                parent_id = id_offset + partition_idx
                child_ids = [comm_ids[i] for i in member_indices]
                total_nodes = sum(current_communities[cid].node_count for cid in child_ids)
                new_comm = CommunityData(
                    id=parent_id,
                    node_count=total_nodes,
                    level=next_level,
                    parent_community_id=None,  # set in the next iteration
                )
                new_communities[parent_id] = new_comm
                for cid in child_ids:
                    child_to_parent[cid] = parent_id

            # Update the child communities' parent_community_id
            updated_children = []
            for cid, child in current_communities.items():
                parent_id = child_to_parent.get(cid)
                if parent_id is not None:
                    updated_children.append(CommunityData(
                        id=child.id,
                        title=child.title,
                        summary=child.summary,
                        node_count=child.node_count,
                        key_entry_points=child.key_entry_points,
                        generated_at=child.generated_at,
                        created_at=child.created_at,
                        level=current_level,
                        parent_community_id=parent_id,
                    ))
            # Parents must be written BEFORE children set parent_community_id.
            # With PRAGMA foreign_keys=ON the UPDATE on children would raise
            # FOREIGN KEY constraint failed if the parent row doesn't exist yet.
            storage.upsert_communities_batch(list(new_communities.values()))
            if updated_children:
                storage.upsert_communities_batch(updated_children)
            levels_built += 1

            log.info(
                "hierarchy: level %d → %d communities from %d",
                next_level, len(new_communities), len(current_communities),
            )

            # Prepare for next iteration: the new level becomes the current level
            current_communities = new_communities
            # Update node_to_community to map to new higher-level IDs
            new_node_to_community: dict[str, int] = {}
            for node_id, old_cid in node_to_community.items():
                parent = child_to_parent.get(old_cid)
                new_node_to_community[node_id] = parent if parent is not None else old_cid
            node_to_community = new_node_to_community

        log.info("hierarchy: built %d additional levels", levels_built)
        return levels_built


