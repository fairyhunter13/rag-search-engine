"""MCP tool matrix — all 5 tools × all variants not already in test_p5_server.

Covers (no duplication of test_p5 or test_p21):
  - graph: all 7 relations (definition/callers/callees/impact/impact_narrative/path/semantic_trace)
  - search: 3 scopes (code/docs/all) + federated project_paths
  - ask: scope variants (architecture/global/feature/wiki/business)
  - overview: patterns / metrics / projects (the 3 what= values that fail on stale index)

Requires daemon at :8765 with ≥1 indexed project.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from opencode_search.core.config import project_graph_db, project_vector_db
from opencode_search.core.registry import list_projects

pytestmark = pytest.mark.live

_GRAPH_RELATIONS_SIMPLE = ["definition", "callers", "callees", "impact", "impact_narrative"]
_ASK_SCOPES = ["architecture", "global", "feature", "wiki", "business"]


@pytest.fixture(scope="module")
def indexed_proj():
    p = next(
        (e for e in list_projects() if e.enabled and project_vector_db(e.path).exists()),
        None,
    )
    assert p, "At least one indexed project required — run Workstream E"
    return p.path


@pytest.fixture(scope="module")
def graph_proj():
    # Require symbols > 0 so thin federation roots (0 own symbols) are skipped — graph
    # relation tests need an actual symbol to look up via any_symbol.
    for e in list_projects():
        if not e.enabled:
            continue
        gdb = project_graph_db(e.path)
        if not gdb.exists():
            continue
        with sqlite3.connect(str(gdb)) as con:
            if con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] > 0:
                return e.path
    pytest.fail("No project with graph.db and symbols required — run Workstream E")


@pytest.fixture(scope="module")
def any_symbol(graph_proj):
    con = sqlite3.connect(str(project_graph_db(graph_proj)))
    row = con.execute("SELECT name FROM symbols LIMIT 1").fetchone()
    con.close()
    assert row, f"graph_proj {graph_proj!r} has no symbols"
    return row[0]


class TestGraphRelations:

    @pytest.mark.parametrize("relation", _GRAPH_RELATIONS_SIMPLE)
    def test_graph_relation_returns_dict(self, graph_proj, any_symbol, relation):
        from opencode_search.server.mcp import graph as graph_tool
        result = asyncio.run(graph_tool(any_symbol, graph_proj, relation))
        data = json.loads(result)
        assert isinstance(data, dict), f"graph({relation!r}) must return JSON object"

    def test_graph_path_without_to_symbol_returns_error_or_empty(self, graph_proj, any_symbol):
        from opencode_search.server.mcp import graph as graph_tool
        result = asyncio.run(graph_tool(any_symbol, graph_proj, "path"))
        data = json.loads(result)
        assert "error" in data or "path" in data

    def test_graph_semantic_trace_without_to_symbol(self, graph_proj, any_symbol):
        from opencode_search.server.mcp import graph as graph_tool
        result = asyncio.run(graph_tool(any_symbol, graph_proj, "semantic_trace"))
        assert isinstance(json.loads(result), dict)

    def test_graph_nonexistent_returns_error(self):
        from opencode_search.server.mcp import graph as graph_tool
        data = json.loads(asyncio.run(graph_tool("foo", "/nonexistent", "definition")))
        assert "error" in data


class TestSearchScopes:

    @pytest.mark.parametrize("scope", ["code", "docs", "all"])
    def test_search_scope(self, indexed_proj, scope):
        from opencode_search.server.mcp import search as search_tool
        data = json.loads(asyncio.run(
            search_tool("function", scope=scope, project_paths=[indexed_proj])
        ))
        assert "total" in data and "results" in data, f"search(scope={scope!r}) missing keys"
        assert isinstance(data["results"], list)

    def test_search_federated_project_paths(self, indexed_proj):
        from opencode_search.server.mcp import search as search_tool
        data = json.loads(asyncio.run(
            search_tool("function", project_paths=[indexed_proj])
        ))
        assert indexed_proj in data.get("projects_searched", [])


class TestAskScopes:

    @pytest.mark.slow
    @pytest.mark.parametrize("scope", _ASK_SCOPES)
    def test_ask_scope_returns_string(self, indexed_proj, scope):
        from opencode_search.server.mcp import ask as ask_tool
        result = asyncio.run(ask_tool("What does this project do?", indexed_proj, scope))
        assert isinstance(result, str) and len(result) > 0, f"ask(scope={scope!r}) returned empty"


class TestOverviewNewWhats:

    @pytest.mark.parametrize("what", ["metrics", "projects"])
    def test_overview_what_returns_nonempty_dict(self, indexed_proj, what):
        from opencode_search.server.mcp import overview as overview_tool
        data = json.loads(asyncio.run(overview_tool(indexed_proj, what)))
        assert isinstance(data, dict), f"overview(what={what!r}) must return JSON object"
        assert data, f"overview(what={what!r}) must not return empty dict"

    @pytest.mark.slow
    def test_overview_patterns_returns_nonempty_dict(self, indexed_proj):
        from opencode_search.server.mcp import overview as overview_tool
        data = json.loads(asyncio.run(overview_tool(indexed_proj, "patterns")))
        assert isinstance(data, dict), "overview(what='patterns') must return JSON object"
        assert data, "overview(what='patterns') must not return empty dict"
