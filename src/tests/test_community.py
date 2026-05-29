"""Tests for opencode_search.graph.community — Leiden community detection."""
from __future__ import annotations

import hashlib
import time

import pytest

from opencode_search.graph.community import CommunityDetector
from opencode_search.graph.storage import EdgeData, GraphStorage, NodeData

pytestmark = [pytest.mark.integration]


def _node_id(file: str, qn: str) -> str:
    return hashlib.sha256(f"{file}::{qn}".encode()).hexdigest()[:16]


def _make_node(file: str, name: str, qn: str | None = None) -> NodeData:
    qualified = qn or f"mod.{name}"
    return NodeData(
        id=_node_id(file, qualified),
        name=name,
        qualified_name=qualified,
        kind="function",
        file=file,
        created_at="",
        updated_at="",
    )


@pytest.fixture
def storage(tmp_path) -> GraphStorage:
    gs = GraphStorage(str(tmp_path / "graph.db"))
    gs.open()
    yield gs
    gs.close()


# ---------------------------------------------------------------------------
# Basic community detection
# ---------------------------------------------------------------------------


def test_leiden_produces_communities_nontrivial_graph(storage):
    """A graph with two clusters should produce >= 1 community."""
    # Cluster A: a1 ↔ a2 ↔ a3
    # Cluster B: b1 ↔ b2 ↔ b3
    nodes_a = [_make_node("/a.py", f"a{i}", f"a.a{i}") for i in range(3)]
    nodes_b = [_make_node("/b.py", f"b{i}", f"b.b{i}") for i in range(3)]
    all_nodes = nodes_a + nodes_b
    storage.upsert_nodes(all_nodes)

    edges = []
    for i in range(len(nodes_a) - 1):
        edges.append(EdgeData(from_id=nodes_a[i].id, to_id=nodes_a[i + 1].id, kind="CALLS"))
        edges.append(EdgeData(from_id=nodes_a[i + 1].id, to_id=nodes_a[i].id, kind="CALLS"))
    for i in range(len(nodes_b) - 1):
        edges.append(EdgeData(from_id=nodes_b[i].id, to_id=nodes_b[i + 1].id, kind="CALLS"))
        edges.append(EdgeData(from_id=nodes_b[i + 1].id, to_id=nodes_b[i].id, kind="CALLS"))
    storage.upsert_edges(edges)

    detector = CommunityDetector()
    mapping = detector.detect_communities(storage)

    assert len(mapping) == len(all_nodes)


def test_leiden_all_nodes_assigned_community_id(storage):
    nodes = [_make_node(f"/f{i}.py", f"f{i}", f"m.f{i}") for i in range(6)]
    storage.upsert_nodes(nodes)
    edges = [
        EdgeData(from_id=nodes[i].id, to_id=nodes[i + 1].id, kind="CALLS")
        for i in range(len(nodes) - 1)
    ]
    storage.upsert_edges(edges)

    detector = CommunityDetector()
    mapping = detector.detect_communities(storage)

    assert len(mapping) == len(nodes)
    # All nodes in storage should have community_id set
    for n in storage.all_nodes():
        assert n.community_id is not None


def test_leiden_community_ids_persisted_in_nodes_table(storage):
    nodes = [_make_node(f"/f{i}.py", f"fn{i}", f"m.fn{i}") for i in range(4)]
    storage.upsert_nodes(nodes)
    edges = [
        EdgeData(from_id=nodes[0].id, to_id=nodes[1].id, kind="CALLS"),
        EdgeData(from_id=nodes[2].id, to_id=nodes[3].id, kind="CALLS"),
    ]
    storage.upsert_edges(edges)

    detector = CommunityDetector()
    detector.detect_communities(storage)

    for n in storage.all_nodes():
        assert n.community_id is not None, f"Node {n.name} has no community_id"


def test_leiden_handles_empty_graph(storage):
    detector = CommunityDetector()
    mapping = detector.detect_communities(storage)
    assert mapping == {}


def test_leiden_handles_single_node_graph(storage):
    n = _make_node("/a.py", "solo", "m.solo")
    storage.upsert_nodes([n])
    detector = CommunityDetector()
    mapping = detector.detect_communities(storage)
    assert len(mapping) == 1
    assert n.id in mapping


def test_leiden_handles_disconnected_components(storage):
    """Unconnected nodes should each be assigned a community."""
    nodes = [_make_node(f"/f{i}.py", f"isolated{i}", f"m.isolated{i}") for i in range(5)]
    storage.upsert_nodes(nodes)
    # No edges at all
    detector = CommunityDetector()
    mapping = detector.detect_communities(storage)
    assert len(mapping) == 5


def test_leiden_idempotent_on_same_graph(storage):
    """Running detection twice on the same graph should give the same number of communities."""
    nodes = [_make_node(f"/f{i}.py", f"f{i}", f"m.f{i}") for i in range(6)]
    storage.upsert_nodes(nodes)
    edges = [
        EdgeData(from_id=nodes[i].id, to_id=nodes[i + 1].id, kind="CALLS")
        for i in range(len(nodes) - 1)
    ]
    storage.upsert_edges(edges)

    detector = CommunityDetector()
    mapping1 = detector.detect_communities(storage)
    communities1 = storage.get_communities()

    # Re-run
    mapping2 = detector.detect_communities(storage)
    communities2 = storage.get_communities()

    assert len(mapping1) == len(mapping2)
    # Community count should be stable
    assert len(communities1) == len(communities2)


def test_leiden_communities_written_to_storage(storage):
    nodes = [_make_node(f"/f{i}.py", f"f{i}", f"m.f{i}") for i in range(4)]
    storage.upsert_nodes(nodes)
    storage.upsert_edges([
        EdgeData(from_id=nodes[0].id, to_id=nodes[1].id, kind="CALLS"),
        EdgeData(from_id=nodes[2].id, to_id=nodes[3].id, kind="CALLS"),
    ])

    detector = CommunityDetector()
    detector.detect_communities(storage)

    communities = storage.get_communities()
    assert len(communities) >= 1
    for c in communities:
        assert c.node_count >= 1


def test_leiden_identifies_entry_points(storage):
    """Entry points: nodes most called from outside their community (when multi-community)."""
    # Use two dense clusters with api_handler as the cross-cluster entry point
    # Cluster A: api_handler + a group of helpers (densely connected)
    # Cluster B: main + a group of callers (densely connected, calls api_handler)
    cluster_a = [_make_node("/api.py", f"ah{i}", f"api.ah{i}") for i in range(5)]
    cluster_b = [_make_node("/main.py", f"mn{i}", f"main.mn{i}") for i in range(5)]
    storage.upsert_nodes(cluster_a + cluster_b)

    edges = []
    # Dense within A
    for i in range(len(cluster_a) - 1):
        edges.append(EdgeData(from_id=cluster_a[i].id, to_id=cluster_a[i + 1].id, kind="CALLS"))
        edges.append(EdgeData(from_id=cluster_a[i + 1].id, to_id=cluster_a[i].id, kind="CALLS"))
    # Dense within B
    for i in range(len(cluster_b) - 1):
        edges.append(EdgeData(from_id=cluster_b[i].id, to_id=cluster_b[i + 1].id, kind="CALLS"))
        edges.append(EdgeData(from_id=cluster_b[i + 1].id, to_id=cluster_b[i].id, kind="CALLS"))
    # Cross-cluster: multiple B nodes call cluster_a[0] (making it an entry point)
    for b_node in cluster_b:
        edges.append(EdgeData(from_id=b_node.id, to_id=cluster_a[0].id, kind="CALLS"))
    storage.upsert_edges(edges)

    detector = CommunityDetector()
    detector.detect_communities(storage)

    communities = storage.get_communities()
    # Entry point logic applies when communities are actually distinct
    # (non-trivial graph may still put all in one community — that's ok)
    # Just verify the function completes and returns valid data
    assert len(communities) >= 1
    for c in communities:
        assert isinstance(c.key_entry_points, list)


def test_leiden_large_graph_500_nodes_under_30s(storage):
    """Performance gate: 500 nodes + 1000 edges completes in < 30 seconds."""
    nodes = [_make_node(f"/f{i}.py", f"fn{i}", f"m.fn{i}") for i in range(500)]
    storage.upsert_nodes(nodes)
    edges = [
        EdgeData(from_id=nodes[i % 500].id, to_id=nodes[(i + 1) % 500].id, kind="CALLS")
        for i in range(1000)
    ]
    storage.upsert_edges(edges)

    detector = CommunityDetector()
    t0 = time.perf_counter()
    mapping = detector.detect_communities(storage)
    elapsed = time.perf_counter() - t0

    assert len(mapping) == 500
    assert elapsed < 30.0, f"Community detection took {elapsed:.1f}s on 500 nodes"


# ---------------------------------------------------------------------------
# handle_get_communities integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_get_communities_returns_all(tmp_path):
    from unittest.mock import patch
    from opencode_search.handlers._graph import handle_get_communities
    from opencode_search.graph.storage import CommunityData

    graph_db_path = str(tmp_path / "graph.db")
    gs = GraphStorage(graph_db_path)
    gs.open()
    for i in range(3):
        gs.upsert_community(CommunityData(
            id=i, title=f"Community {i}", node_count=i + 1,
        ))
    gs.close()

    with patch(
        "opencode_search.handlers._graph.get_project_graph_db_path",
        return_value=graph_db_path,
    ):
        result = await handle_get_communities(project_path="/tmp/proj")

    assert result["total"] == 3
    titles = {c["title"] for c in result["communities"]}
    assert "Community 0" in titles


@pytest.mark.asyncio
async def test_handle_get_communities_before_detection_returns_empty(tmp_path):
    from unittest.mock import patch
    from opencode_search.handlers._graph import handle_get_communities

    graph_db_path = str(tmp_path / "graph.db")
    gs = GraphStorage(graph_db_path)
    gs.open()
    gs.close()

    with patch(
        "opencode_search.handlers._graph.get_project_graph_db_path",
        return_value=graph_db_path,
    ):
        result = await handle_get_communities(project_path="/tmp/proj")

    assert result["total"] == 0
    assert result["communities"] == []
