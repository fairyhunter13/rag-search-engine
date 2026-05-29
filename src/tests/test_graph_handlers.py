"""Tests for opencode_search.handlers._graph — graph MCP handlers."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from opencode_search.graph.storage import EdgeData, GraphStorage, NodeData
from opencode_search.handlers._graph import (
    handle_detect_impact,
    handle_get_callers,
    handle_get_callees,
    handle_get_communities,
    handle_get_symbol,
    handle_trace_path,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _node_id(file: str, qn: str) -> str:
    return hashlib.sha256(f"{file}::{qn}".encode()).hexdigest()[:16]


def _make_node(file: str, name: str, qualified_name: str | None = None) -> NodeData:
    qn = qualified_name or f"mod.{name}"
    return NodeData(
        id=_node_id(file, qn),
        name=name,
        qualified_name=qn,
        kind="function",
        file=file,
        start_line=1,
        end_line=10,
        language="python",
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )


@pytest.fixture
def project_with_graph(tmp_path):
    """Create a temp project with a pre-populated graph DB."""
    from opencode_search import config
    import unittest.mock as mock

    project_root = tmp_path / "project"
    project_root.mkdir()

    # Patch config to use tmp path
    graph_db_path = str(tmp_path / "graph.db")

    gs = GraphStorage(graph_db_path)
    gs.open()

    nodes = [
        _make_node("/project/auth.py", "authenticate", "auth.authenticate"),
        _make_node("/project/auth.py", "verify_token", "auth.verify_token"),
        _make_node("/project/handler.py", "handle_login", "handler.handle_login"),
        _make_node("/project/db.py", "get_connection", "db.get_connection"),
    ]
    gs.upsert_nodes(nodes)

    # handle_login calls authenticate, authenticate calls verify_token
    gs.upsert_edges([
        EdgeData(
            from_id=nodes[2].id,  # handle_login
            to_id=nodes[0].id,    # authenticate
            kind="CALLS",
            confidence=0.9,
            resolution_strategy="same_module",
        ),
        EdgeData(
            from_id=nodes[0].id,  # authenticate
            to_id=nodes[1].id,    # verify_token
            kind="CALLS",
            confidence=0.85,
            resolution_strategy="unique_name",
        ),
    ])
    gs.close()

    # Patch get_project_graph_db_path to return our test path
    with mock.patch(
        "opencode_search.handlers._graph.get_project_graph_db_path",
        return_value=graph_db_path,
    ):
        yield str(project_root), nodes, graph_db_path


async def test_handle_get_symbol_found_by_name(project_with_graph):
    project_path, nodes, _ = project_with_graph
    result = await handle_get_symbol(name="authenticate", project_path=project_path)
    assert "matches" in result
    assert result["count"] >= 1
    match = result["matches"][0]
    assert match["name"] == "authenticate"
    assert match["kind"] == "function"
    assert match["file"] == "/project/auth.py"


async def test_handle_get_symbol_found_by_qualified_name(project_with_graph):
    project_path, _, _ = project_with_graph
    result = await handle_get_symbol(name="auth.authenticate", project_path=project_path)
    assert result["count"] >= 1
    assert result["matches"][0]["qualified_name"] == "auth.authenticate"


async def test_handle_get_symbol_not_found_returns_error_dict(project_with_graph):
    project_path, _, _ = project_with_graph
    result = await handle_get_symbol(name="nonexistent_xyz", project_path=project_path)
    assert "error" in result


async def test_handle_get_symbol_includes_caller_count(project_with_graph):
    project_path, nodes, _ = project_with_graph
    # authenticate is called by handle_login
    result = await handle_get_symbol(name="authenticate", project_path=project_path)
    match = result["matches"][0]
    assert match["caller_count"] >= 1


async def test_handle_get_callers_returns_chain(project_with_graph):
    project_path, nodes, _ = project_with_graph
    # verify_token is called by authenticate
    result = await handle_get_callers(
        symbol="verify_token", project_path=project_path, depth=2,
    )
    assert "callers" in result
    assert result["total"] >= 1
    caller_names = {c["name"] for c in result["callers"]}
    assert "authenticate" in caller_names


async def test_handle_get_callers_respects_depth_param(project_with_graph):
    project_path, nodes, _ = project_with_graph
    result = await handle_get_callers(
        symbol="verify_token", project_path=project_path, depth=1,
    )
    for c in result["callers"]:
        assert c["depth"] <= 1


async def test_handle_get_callers_symbol_not_found(project_with_graph):
    project_path, _, _ = project_with_graph
    result = await handle_get_callers(symbol="ghost_fn", project_path=project_path)
    assert "error" in result


async def test_handle_get_callees_returns_chain(project_with_graph):
    project_path, nodes, _ = project_with_graph
    result = await handle_get_callees(
        symbol="authenticate", project_path=project_path, depth=2,
    )
    assert "callees" in result
    assert result["total"] >= 1
    callee_names = {c["name"] for c in result["callees"]}
    assert "verify_token" in callee_names


async def test_handle_trace_path_returns_ordered_steps(project_with_graph):
    project_path, nodes, _ = project_with_graph
    result = await handle_trace_path(
        from_symbol="handle_login",
        to_symbol="verify_token",
        project_path=project_path,
    )
    assert result.get("connected") is True
    assert len(result["path"]) >= 2
    assert result["hops"] >= 1


async def test_handle_trace_path_no_connection_returns_empty(project_with_graph):
    project_path, nodes, _ = project_with_graph
    # get_connection has no edges
    result = await handle_trace_path(
        from_symbol="get_connection",
        to_symbol="authenticate",
        project_path=project_path,
    )
    assert result.get("connected") is False or "path" in result


async def test_handle_detect_impact_grouped_by_depth(project_with_graph):
    project_path, nodes, _ = project_with_graph
    # verify_token is at the bottom; handle_login → authenticate → verify_token
    result = await handle_detect_impact(symbol="verify_token", project_path=project_path)
    assert "callers_by_depth" in result
    assert result["total_affected"] >= 1


async def test_handle_detect_impact_empty_when_leaf_node(project_with_graph):
    project_path, nodes, _ = project_with_graph
    # get_connection has no callers
    result = await handle_detect_impact(symbol="get_connection", project_path=project_path)
    assert result.get("total_affected", 0) == 0


async def test_handle_get_communities_returns_list(project_with_graph):
    project_path, _, graph_db_path = project_with_graph
    import unittest.mock as mock
    from opencode_search.graph.storage import CommunityData

    gs = GraphStorage(graph_db_path)
    gs.open()
    gs.upsert_community(CommunityData(
        id=0, title="Auth layer", summary="Handles auth",
        node_count=3, key_entry_points=["auth.authenticate"],
    ))
    gs.close()

    result = await handle_get_communities(project_path=project_path)
    assert "communities" in result
    assert result["total"] >= 1
    assert result["communities"][0]["title"] == "Auth layer"


async def test_handle_get_symbol_no_graph_returns_error(tmp_path):
    """When graph DB doesn't exist, return error dict instead of crashing."""
    import unittest.mock as mock

    with mock.patch(
        "opencode_search.handlers._graph.get_project_graph_db_path",
        return_value=str(tmp_path / "nonexistent.db"),
    ):
        result = await handle_get_symbol(name="anything", project_path="/tmp/nonexistent")
    assert "error" in result
