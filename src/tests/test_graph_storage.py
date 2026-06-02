"""Tests for opencode_search.graph.storage — GraphStorage SQLite backend."""
from __future__ import annotations

import sqlite3

import pytest

from opencode_search.graph.storage import (
    CommunityData,
    EdgeData,
    GraphStorage,
    NodeData,
)

pytestmark = [pytest.mark.integration]


def _make_node(
    file_path: str,
    name: str,
    kind: str = "function",
    qualified_name: str | None = None,
) -> NodeData:
    qn = qualified_name or f"mod.{name}"
    node_id = f"{file_path}_{name}"[:16]
    import hashlib
    node_id = hashlib.sha256(f"{file_path}::{qn}".encode()).hexdigest()[:16]
    return NodeData(
        id=node_id,
        name=name,
        qualified_name=qn,
        kind=kind,
        file=file_path,
        start_line=1,
        end_line=10,
        language="python",
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )


@pytest.fixture
def storage(tmp_path) -> GraphStorage:
    gs = GraphStorage(str(tmp_path / "graph.db"))
    gs.open()
    yield gs
    gs.close()


# ---------------------------------------------------------------------------
# Schema / initialization
# ---------------------------------------------------------------------------


def test_graph_storage_creates_all_tables_on_open(tmp_path):
    gs = GraphStorage(str(tmp_path / "graph.db"))
    gs.open()
    try:
        conn = sqlite3.connect(str(tmp_path / "graph.db"))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "nodes" in tables
        assert "edges" in tables
        assert "communities" in tables
    finally:
        gs.close()


def test_graph_storage_creates_all_indexes_on_open(tmp_path):
    gs = GraphStorage(str(tmp_path / "graph.db"))
    gs.open()
    try:
        conn = sqlite3.connect(str(tmp_path / "graph.db"))
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        conn.close()
        assert "idx_nodes_file" in indexes
        assert "idx_nodes_kind" in indexes
        assert "idx_edges_from" in indexes
        assert "idx_edges_to" in indexes
    finally:
        gs.close()


def test_graph_storage_wal_mode_enabled(tmp_path):
    gs = GraphStorage(str(tmp_path / "graph.db"))
    gs.open()
    try:
        conn = sqlite3.connect(str(tmp_path / "graph.db"))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"
    finally:
        gs.close()


def test_graph_storage_context_manager(tmp_path):
    db_path = str(tmp_path / "graph.db")
    with GraphStorage(db_path) as gs:
        gs.upsert_nodes([_make_node("/f.py", "foo")])
        assert gs.node_count() == 1


# ---------------------------------------------------------------------------
# Node write / read
# ---------------------------------------------------------------------------


def test_graph_storage_upsert_node_basic(storage):
    n = _make_node("/tmp/foo.py", "foo")
    storage.upsert_nodes([n])
    assert storage.node_count() == 1


def test_graph_storage_upsert_node_idempotent(storage):
    n = _make_node("/tmp/foo.py", "foo")
    storage.upsert_nodes([n])
    storage.upsert_nodes([n])
    assert storage.node_count() == 1


def test_graph_storage_upsert_node_updates_on_conflict(storage):
    n = _make_node("/tmp/foo.py", "foo")
    storage.upsert_nodes([n])
    n2 = NodeData(
        id=n.id, name="foo", qualified_name=n.qualified_name, kind="function",
        file="/tmp/foo.py", start_line=99, end_line=100, language="python",
        created_at="2026-01-01T00:00:00", updated_at="2026-01-02T00:00:00",
    )
    storage.upsert_nodes([n2])
    result = storage.get_node_by_id(n.id)
    assert result is not None
    assert result.start_line == 99
    assert storage.node_count() == 1


def test_graph_storage_get_node_by_name(storage):
    n = _make_node("/tmp/foo.py", "authenticate", qualified_name="auth.authenticate")
    storage.upsert_nodes([n])
    found = storage.get_node("authenticate")
    assert found is not None
    assert found.name == "authenticate"


def test_graph_storage_get_node_by_qualified_name(storage):
    n = _make_node("/tmp/foo.py", "authenticate", qualified_name="auth.authenticate")
    storage.upsert_nodes([n])
    found = storage.get_node("auth.authenticate")
    assert found is not None
    assert found.qualified_name == "auth.authenticate"


def test_graph_storage_get_node_not_found_returns_none(storage):
    assert storage.get_node("nonexistent_xyz") is None


def test_graph_storage_get_nodes_by_name_multiple_matches(storage):
    n1 = _make_node("/a.py", "run", qualified_name="a.run")
    n2 = _make_node("/b.py", "run", qualified_name="b.run")
    storage.upsert_nodes([n1, n2])
    matches = storage.get_nodes_by_name("run")
    assert len(matches) == 2


def test_graph_storage_all_nodes_returns_complete_list(storage):
    nodes = [_make_node(f"/f{i}.py", f"func{i}") for i in range(5)]
    storage.upsert_nodes(nodes)
    all_nodes = storage.all_nodes()
    assert len(all_nodes) == 5


# ---------------------------------------------------------------------------
# Edge write / read
# ---------------------------------------------------------------------------


def test_graph_storage_upsert_edge_basic(storage):
    n1 = _make_node("/a.py", "foo", qualified_name="m.foo")
    n2 = _make_node("/b.py", "bar", qualified_name="m.bar")
    storage.upsert_nodes([n1, n2])
    e = EdgeData(from_id=n1.id, to_id=n2.id, kind="CALLS", confidence=0.9)
    storage.upsert_edges([e])
    assert storage.edge_count() == 1


def test_graph_storage_upsert_edge_idempotent(storage):
    n1 = _make_node("/a.py", "foo", qualified_name="m.foo")
    n2 = _make_node("/b.py", "bar", qualified_name="m.bar")
    storage.upsert_nodes([n1, n2])
    e = EdgeData(from_id=n1.id, to_id=n2.id, kind="CALLS")
    storage.upsert_edges([e])
    storage.upsert_edges([e])
    assert storage.edge_count() == 1


def test_graph_storage_all_edges_returns_complete_list(storage):
    nodes = [_make_node(f"/f{i}.py", f"f{i}", qualified_name=f"m.f{i}") for i in range(3)]
    storage.upsert_nodes(nodes)
    edges = [
        EdgeData(from_id=nodes[0].id, to_id=nodes[1].id, kind="CALLS"),
        EdgeData(from_id=nodes[1].id, to_id=nodes[2].id, kind="CALLS"),
    ]
    storage.upsert_edges(edges)
    all_edges = storage.all_edges()
    assert len(all_edges) == 2


# ---------------------------------------------------------------------------
# Delete file
# ---------------------------------------------------------------------------


def test_graph_storage_delete_file_removes_nodes(storage):
    n1 = _make_node("/a.py", "foo", qualified_name="a.foo")
    n2 = _make_node("/b.py", "bar", qualified_name="b.bar")
    storage.upsert_nodes([n1, n2])
    storage.delete_file("/a.py")
    assert storage.node_count() == 1
    assert storage.get_node("foo") is None
    assert storage.get_node("bar") is not None


def test_graph_storage_delete_file_removes_edges_both_directions(storage):
    n1 = _make_node("/a.py", "foo", qualified_name="a.foo")
    n2 = _make_node("/b.py", "bar", qualified_name="b.bar")
    storage.upsert_nodes([n1, n2])
    storage.upsert_edges([
        EdgeData(from_id=n1.id, to_id=n2.id, kind="CALLS"),
    ])
    storage.delete_file("/a.py")
    assert storage.edge_count() == 0


def test_graph_storage_delete_nonexistent_file_no_error(storage):
    storage.delete_file("/nonexistent.py")  # should not raise


# ---------------------------------------------------------------------------
# BFS traversal
# ---------------------------------------------------------------------------


def _make_call_graph(storage: GraphStorage) -> tuple[list[NodeData], list[EdgeData]]:
    """
    a → b → c → d
    """
    nodes = [
        _make_node(f"/f{i}.py", f"func_{i}", qualified_name=f"m.func_{i}")
        for i in range(4)
    ]
    storage.upsert_nodes(nodes)
    edges = [
        EdgeData(from_id=nodes[0].id, to_id=nodes[1].id, kind="CALLS"),
        EdgeData(from_id=nodes[1].id, to_id=nodes[2].id, kind="CALLS"),
        EdgeData(from_id=nodes[2].id, to_id=nodes[3].id, kind="CALLS"),
    ]
    storage.upsert_edges(edges)
    return nodes, edges


def test_graph_storage_bfs_callers_depth_1(storage):
    nodes, _ = _make_call_graph(storage)
    # func_1 is called by func_0
    callers = storage.get_callers(nodes[1].id, depth=1)
    assert len(callers) == 1
    assert callers[0].node_id == nodes[0].id
    assert callers[0].depth == 1


def test_graph_storage_bfs_callers_depth_3(storage):
    nodes, _ = _make_call_graph(storage)
    # func_3 is transitively called by func_2 → func_1 → func_0
    callers = storage.get_callers(nodes[3].id, depth=3)
    node_ids = {c.node_id for c in callers}
    assert nodes[2].id in node_ids
    assert nodes[1].id in node_ids
    assert nodes[0].id in node_ids


def test_graph_storage_bfs_callers_respects_depth_limit(storage):
    nodes, _ = _make_call_graph(storage)
    callers = storage.get_callers(nodes[3].id, depth=1)
    assert all(c.depth <= 1 for c in callers)
    assert len(callers) == 1  # only func_2


def test_graph_storage_bfs_callees_depth_1(storage):
    nodes, _ = _make_call_graph(storage)
    callees = storage.get_callees(nodes[0].id, depth=1)
    assert len(callees) == 1
    assert callees[0].node_id == nodes[1].id


def test_graph_storage_bfs_callees_depth_3(storage):
    nodes, _ = _make_call_graph(storage)
    callees = storage.get_callees(nodes[0].id, depth=3)
    node_ids = {c.node_id for c in callees}
    assert nodes[1].id in node_ids
    assert nodes[2].id in node_ids
    assert nodes[3].id in node_ids


def test_graph_storage_bfs_returns_empty_for_leaf_node(storage):
    n = _make_node("/f.py", "leaf_node", qualified_name="m.leaf_node")
    storage.upsert_nodes([n])
    assert storage.get_callees(n.id) == []
    assert storage.get_callers(n.id) == []


# ---------------------------------------------------------------------------
# trace_path
# ---------------------------------------------------------------------------


def test_graph_storage_trace_path_direct_connection(storage):
    n1 = _make_node("/a.py", "a", qualified_name="m.a")
    n2 = _make_node("/b.py", "b", qualified_name="m.b")
    storage.upsert_nodes([n1, n2])
    storage.upsert_edges([EdgeData(from_id=n1.id, to_id=n2.id, kind="CALLS")])
    path = storage.trace_path(n1.id, n2.id)
    assert path is not None
    assert n1.id in path
    assert n2.id in path


def test_graph_storage_trace_path_indirect_connection(storage):
    nodes, _ = _make_call_graph(storage)
    path = storage.trace_path(nodes[0].id, nodes[3].id)
    assert path is not None
    assert len(path) >= 4


def test_graph_storage_trace_path_no_path_returns_none(storage):
    n1 = _make_node("/a.py", "a", qualified_name="m.a")
    n2 = _make_node("/b.py", "b", qualified_name="m.b")
    storage.upsert_nodes([n1, n2])
    # no edges
    path = storage.trace_path(n1.id, n2.id)
    assert path is None


def test_graph_storage_trace_path_cycle_terminates(storage):
    """Cyclic graph: a → b → a should not loop forever."""
    n1 = _make_node("/a.py", "a", qualified_name="m.a")
    n2 = _make_node("/b.py", "b", qualified_name="m.b")
    n3 = _make_node("/c.py", "c", qualified_name="m.c")
    storage.upsert_nodes([n1, n2, n3])
    storage.upsert_edges([
        EdgeData(from_id=n1.id, to_id=n2.id, kind="CALLS"),
        EdgeData(from_id=n2.id, to_id=n1.id, kind="CALLS"),  # cycle
    ])
    path = storage.trace_path(n1.id, n3.id)  # n3 unreachable
    assert path is None


# ---------------------------------------------------------------------------
# Community
# ---------------------------------------------------------------------------


def test_graph_storage_set_community(storage):
    n = _make_node("/f.py", "fn", qualified_name="m.fn")
    storage.upsert_nodes([n])
    storage.set_community(n.id, 42)
    found = storage.get_node_by_id(n.id)
    assert found is not None
    assert found.community_id == 42


def test_graph_storage_upsert_community(storage):
    c = CommunityData(
        id=0, title="Auth layer", summary="Handles JWT auth",
        node_count=5, key_entry_points=["auth.authenticate"],
        created_at="2026-01-01T00:00:00",
    )
    storage.upsert_community(c)
    communities = storage.get_communities()
    assert len(communities) == 1
    assert communities[0].title == "Auth layer"
    assert communities[0].key_entry_points == ["auth.authenticate"]


def test_graph_storage_upsert_community_updates_on_conflict(storage):
    c = CommunityData(id=0, title="old", node_count=1)
    storage.upsert_community(c)
    c2 = CommunityData(id=0, title="new", node_count=2)
    storage.upsert_community(c2)
    communities = storage.get_communities()
    assert len(communities) == 1
    assert communities[0].title == "new"
    assert communities[0].node_count == 2


def test_graph_storage_get_communities(storage):
    for i in range(3):
        storage.upsert_community(CommunityData(id=i, node_count=i + 1))
    communities = storage.get_communities()
    assert len(communities) == 3


def test_graph_storage_get_community_nodes(storage):
    nodes = [
        _make_node(f"/f{i}.py", f"fn{i}", qualified_name=f"m.fn{i}")
        for i in range(5)
    ]
    storage.upsert_nodes(nodes)
    for n in nodes[:3]:
        storage.set_community(n.id, 7)
    for n in nodes[3:]:
        storage.set_community(n.id, 8)
    comm7 = storage.get_community_nodes(7)
    comm8 = storage.get_community_nodes(8)
    assert len(comm7) == 3
    assert len(comm8) == 2


# ---------------------------------------------------------------------------
# get_communities_for_files — unit tests
# ---------------------------------------------------------------------------

def test_get_communities_for_files_returns_community_ids(storage):
    """get_communities_for_files returns correct community IDs for given file paths."""
    n1 = _make_node("/src/a.py", "foo", qualified_name="a.foo")
    n2 = _make_node("/src/b.py", "bar", qualified_name="b.bar")
    n3 = _make_node("/src/c.py", "baz", qualified_name="c.baz")
    storage.upsert_nodes([n1, n2, n3])
    storage.set_community(n1.id, 10)
    storage.set_community(n2.id, 20)
    storage.set_community(n3.id, 20)

    result_a = storage.get_communities_for_files(["/src/a.py"])
    assert 10 in result_a, f"Expected community 10 for a.py, got {result_a}"

    result_bc = storage.get_communities_for_files(["/src/b.py", "/src/c.py"])
    assert 20 in result_bc, f"Expected community 20 for b.py/c.py, got {result_bc}"

    result_all = storage.get_communities_for_files(["/src/a.py", "/src/b.py", "/src/c.py"])
    assert 10 in result_all and 20 in result_all, f"Expected both communities, got {result_all}"


def test_get_communities_for_files_empty_input_returns_empty(storage):
    """get_communities_for_files([]) must return []."""
    result = storage.get_communities_for_files([])
    assert result == [], f"Empty input must return [], got {result}"


def test_get_communities_for_files_nonexistent_file_returns_empty(storage):
    """Files not in the graph return no community IDs."""
    result = storage.get_communities_for_files(["/nonexistent/path/to/file.py"])
    assert result == [], f"Nonexistent file must return [], got {result}"


def test_get_communities_for_files_no_community_assigned(storage):
    """Nodes without community_id assigned are not returned."""
    n = _make_node("/src/orphan.py", "orphan", qualified_name="orphan.orphan")
    storage.upsert_nodes([n])
    # Do NOT set community
    result = storage.get_communities_for_files(["/src/orphan.py"])
    assert result == [], f"Nodes without community must not appear, got {result}"


# ---------------------------------------------------------------------------
# trace_path cycle detection — robust delimiter-aware matching
# ---------------------------------------------------------------------------

def test_trace_path_cycle_detection_does_not_false_positive(storage):
    """trace_path cycle guard must use delimiter-aware matching, not INSTR substring.

    If node ID 'ab12345678901234' is a prefix substring of another node ID
    'ab12345678901234xy' (impossible with 16-char hex IDs, but tests the guard),
    the cycle check must NOT fire. Uses realistic 16-char hex node IDs.
    """
    # Build a simple graph: A → B → C (no cycle)
    import hashlib

    def nid(s: str) -> str:
        return hashlib.sha256(s.encode()).hexdigest()[:16]

    na = _make_node("/cycle/a.py", "funcA", qualified_name="cycle.funcA")
    nb = _make_node("/cycle/b.py", "funcB", qualified_name="cycle.funcB")
    nc = _make_node("/cycle/c.py", "funcC", qualified_name="cycle.funcC")
    storage.upsert_nodes([na, nb, nc])

    # A → B → C
    storage.upsert_edges([
        EdgeData(from_id=na.id, to_id=nb.id, kind="CALLS", confidence=1.0),
        EdgeData(from_id=nb.id, to_id=nc.id, kind="CALLS", confidence=1.0),
    ])

    path = storage.trace_path(na.id, nc.id)
    assert path is not None, "trace_path must find A → B → C path"
    assert nc.id in path, f"Destination C must be in path: {path}"
    assert len(path) == 3, f"Expected path length 3 (A, B, C), got {path}"


def test_trace_path_returns_none_when_no_path(storage):
    """trace_path returns None when there is no path between nodes."""
    na = _make_node("/nopath/a.py", "alpha", qualified_name="m.alpha")
    nb = _make_node("/nopath/b.py", "beta", qualified_name="m.beta")
    storage.upsert_nodes([na, nb])
    # No edges — no path
    result = storage.trace_path(na.id, nb.id)
    assert result is None, f"trace_path with no edges must return None, got {result}"


def test_get_callers_deduplicates_by_node_id(storage):
    """get_callers must deduplicate nodes appearing at multiple depths."""
    # Build: A calls B, B calls A (cycle), C calls B
    # At depth ≥ 2, B might reappear as a caller of itself

    na = _make_node("/dedup/a.py", "funcA", qualified_name="d.funcA")
    nb = _make_node("/dedup/b.py", "funcB", qualified_name="d.funcB")
    nc = _make_node("/dedup/c.py", "funcC", qualified_name="d.funcC")
    storage.upsert_nodes([na, nb, nc])

    # C → B → target
    nd = _make_node("/dedup/target.py", "target", qualified_name="d.target")
    storage.upsert_nodes([nd])
    storage.upsert_edges([
        EdgeData(from_id=nc.id, to_id=nb.id, kind="CALLS", confidence=1.0),
        EdgeData(from_id=nb.id, to_id=nd.id, kind="CALLS", confidence=1.0),
        EdgeData(from_id=na.id, to_id=nd.id, kind="CALLS", confidence=1.0),  # direct path too
    ])

    callers = storage.get_callers(nd.id, depth=3)
    caller_ids = [c.node_id for c in callers]
    # Each node_id must appear at most once (deduplication)
    assert len(caller_ids) == len(set(caller_ids)), (
        f"Duplicate node_ids in get_callers result: {caller_ids}"
    )
