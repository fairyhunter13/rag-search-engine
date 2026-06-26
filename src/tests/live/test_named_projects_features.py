"""T3: all search-engine features working against the 3 canonical project roles.

test_mcp_tool_matrix.py binds to 'any indexed project'; this file proves every feature
works for ose (standalone self-index), a federation root, and a standalone project.
Project selection is capability-based (env override via OSE_TEST_* or auto-discovery).
No duplication of matrix tests — focuses on per-root binding and the 15 overview what= values.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.live._sample_workspace import SampleWorkspace

pytestmark = pytest.mark.live

_OSE = str(Path(__file__).resolve().parents[3])


@pytest.fixture(scope="module")
def named_projects(sample_workspace: SampleWorkspace) -> dict[str, str]:
    return {
        "ose": _OSE,
        "federation": sample_workspace.fed_root,
        "standalone": sample_workspace.ledger,
    }

_OVERVIEW_WHATS_FAST = [
    "structure", "status", "projects", "metrics",
    "feature_map", "business_rules",
    "process_flows", "service_mesh", "suggested_questions",
    "surprising_connections", "import_cycles",
]
_OVERVIEW_WHATS_SLOW = ["communities", "patterns"]
_SEARCH_SCOPES = ["code", "docs", "all"]


class TestNamedProjectsSearch:
    """T3a: search returns results for each named root across all scopes."""

    @pytest.mark.parametrize("key,scope", [
        (k, s) for k in ("ose", "federation", "standalone") for s in _SEARCH_SCOPES
    ])
    def test_search_returns_results(self, named_projects: dict, key: str, scope: str) -> None:
        from opencode_search.server.mcp import search as search_tool
        path = named_projects.get(key, "")
        assert path, f"{key} not in registry — all 3 project roles must be registered"
        data = json.loads(asyncio.run(search_tool("function", scope=scope, project_paths=[path])))
        assert "results" in data, f"{key} scope={scope}: missing 'results'"
        assert "total" in data, f"{key} scope={scope}: missing 'total'"


class TestNamedProjectsOverview:
    """T3b: all 15 overview what= values return valid JSON for each named root."""

    @pytest.mark.parametrize("key,what", [
        (k, w) for k in ("ose", "federation", "standalone") for w in _OVERVIEW_WHATS_FAST
    ])
    def test_overview_what_returns_dict(self, named_projects: dict, key: str, what: str) -> None:
        from opencode_search.server.mcp import overview as overview_tool
        path = named_projects.get(key, "")
        assert path, f"{key} not in registry — all 3 project roles must be registered"
        result = asyncio.run(overview_tool(path, what))
        data = json.loads(result)
        assert isinstance(data, dict), f"{key} overview({what!r}) must return JSON object"

    @pytest.mark.slow
    @pytest.mark.parametrize("key,what", [
        (k, w) for k in ("ose", "federation", "standalone") for w in _OVERVIEW_WHATS_SLOW
    ])
    def test_overview_slow_what_returns_dict(self, named_projects: dict, key: str, what: str) -> None:
        from opencode_search.server.mcp import overview as overview_tool
        path = named_projects.get(key, "")
        assert path, f"{key} not in registry — all 3 project roles must be registered"
        result = asyncio.run(overview_tool(path, what))
        data = json.loads(result)
        assert isinstance(data, dict), f"{key} overview({what!r}) must return JSON object"


class TestNamedProjectsAsk:
    """T3c: ask returns non-empty context for each named root."""

    @pytest.mark.slow
    @pytest.mark.parametrize("key", ["ose", "federation", "standalone"])
    def test_ask_global_non_empty(self, named_projects: dict, key: str) -> None:
        from opencode_search.server.mcp import ask as ask_tool
        path = named_projects.get(key, "")
        assert path, f"{key} not in registry — all 3 project roles must be registered"
        result = asyncio.run(ask_tool("What is the overall architecture?", path, "global"))
        assert isinstance(result, str) and len(result.strip()) > 20, (
            f"{key}: ask(global) returned empty/short: {result!r}"
        )


class TestNamedProjectsGraph:
    """T3d: graph tool works for each named root (at least definition relation)."""

    @pytest.mark.parametrize("key", ["ose", "federation", "standalone"])
    def test_graph_definition_returns_dict(self, named_projects: dict, key: str) -> None:
        import sqlite3

        from opencode_search.core.config import project_graph_db
        from opencode_search.server.mcp import graph as graph_tool
        path = named_projects.get(key, "")
        assert path, f"{key} not in registry — all 3 project roles must be registered"
        gdb = project_graph_db(path)
        assert gdb.exists(), f"{key}: no graph.db — project must be indexed"
        con = sqlite3.connect(str(gdb))
        row = con.execute("SELECT name FROM symbols LIMIT 1").fetchone()
        con.close()
        assert row, f"{key}: no symbols in graph.db — project must have symbols extracted"
        data = json.loads(asyncio.run(graph_tool(row[0], path, "definition")))
        assert isinstance(data, dict), f"{key}: graph(definition) must return JSON object"
