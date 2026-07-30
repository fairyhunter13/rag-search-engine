"""T3: all search-engine features working against the 3 canonical sample project roles.

test_mcp_tool_matrix.py binds to any indexed project via a single promo-svc scope.
This file proves every feature works for the 3 roles: federation member (promo-svc),
federation root (shop-federation), and standalone (ledger-standalone). All 3 are sourced
from sample_workspace — no real device projects used.
No duplication of matrix tests — focuses on per-root binding across the overview what= values
this file can afford to sweep (see `_OVERVIEW_WHATS_FAST` below for the two it leaves out and
who owns them instead).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from tests.live._sample_workspace import SampleWorkspace

pytestmark = pytest.mark.live

@pytest.fixture(scope="module")
def named_projects(sample_workspace: SampleWorkspace) -> dict[str, str]:
    return {
        "service": sample_workspace.promo,
        "federation": sample_workspace.fed_root,
        "standalone": sample_workspace.ledger,
    }

# feature_map / business_rules / process_flows / service_mesh (fast) and patterns (slow) left
# with tier 3. Worth recording that none of the five went red on their own: these tests assert
# only `isinstance(data, dict)`, and handle_overview's rejection — {"error": "unknown what=…"} —
# is a dict, so the rows stayed green against variants that no longer exist. Whether a deleted
# what= is rejected is asserted by name in test_feature_proof.py's fp1; what this file uniquely
# covers is that a *surviving* what= binds for all three project roles.
# `suggested_questions` was an eighth row here and left with the chat box it seeded. It is not
# swapped for another variant: the two `_VALID` values this list still omits are `communities`
# (already the slow row below) and `validate`, which walks the index and does not belong in a fast
# lane. That gap is asserted deliberately elsewhere — test_feature_proof's fp2 now requires its
# `_WHATS` to equal `_VALID` exactly, which is the list that has to stay complete.
_OVERVIEW_WHATS_FAST = [
    "structure", "status", "projects", "metrics",
    "surprising_connections", "import_cycles",
]
_OVERVIEW_WHATS_SLOW = ["communities"]
_SEARCH_SCOPES = ["code", "docs", "all"]


class TestNamedProjectsSearch:
    """T3a: search returns results for each named root across all scopes."""

    @pytest.mark.parametrize("key,scope", [
        (k, s) for k in ("service", "federation", "standalone") for s in _SEARCH_SCOPES
    ])
    def test_search_returns_results(self, named_projects: dict, key: str, scope: str) -> None:
        from rag_search.server.mcp import search as search_tool
        path = named_projects.get(key, "")
        assert path, f"{key} not in registry — all 3 project roles must be registered"
        data = json.loads(asyncio.run(search_tool("function", scope=scope, project_paths=[path])))
        assert "results" in data, f"{key} scope={scope}: missing 'results'"
        assert "total" in data, f"{key} scope={scope}: missing 'total'"


class TestNamedProjectsOverview:
    """T3b: the fast overview what= values return valid JSON for each named root."""

    @pytest.mark.parametrize("key,what", [
        (k, w) for k in ("service", "federation", "standalone") for w in _OVERVIEW_WHATS_FAST
    ])
    def test_overview_what_returns_dict(self, named_projects: dict, key: str, what: str) -> None:
        from rag_search.server.mcp import overview as overview_tool
        path = named_projects.get(key, "")
        assert path, f"{key} not in registry — all 3 project roles must be registered"
        result = asyncio.run(overview_tool(path, what))
        data = json.loads(result)
        assert isinstance(data, dict), f"{key} overview({what!r}) must return JSON object"

    @pytest.mark.parametrize("key,what", [
        (k, w) for k in ("service", "federation", "standalone") for w in _OVERVIEW_WHATS_SLOW
    ])
    def test_overview_slow_what_returns_dict(self, named_projects: dict, key: str, what: str) -> None:
        from rag_search.server.mcp import overview as overview_tool
        path = named_projects.get(key, "")
        assert path, f"{key} not in registry — all 3 project roles must be registered"
        result = asyncio.run(overview_tool(path, what))
        data = json.loads(result)
        assert isinstance(data, dict), f"{key} overview({what!r}) must return JSON object"


class TestNamedProjectsAsk:
    """T3c: context assembly returns non-empty context for each named root.

    This case was the expensive half of one drift, and worth keeping the record of. It called the
    MCP `ask` tool with scope `"global"`, which 5f3033a removed from `_SCOPES`; `run_ask` answers
    an unlisted scope with a 49-character rejection string, and this assertion asked only for >20
    characters — so all three roles went on passing **against the error message**, proving nothing
    about any of them. The sibling case fp14 demanded >100 and went loudly red on the same day
    from the same cause, which is the whole difference between a threshold that discriminates and
    one that does not ([[feedback_guard_tests_must_discriminate]]). Both are corrected to
    `"architecture"`, and SC3 in test_surface_consistency.py now fails on any future straggler.

    The tool is gone too (retired 2026-07-29); `run_ask` is where the assembly always happened and
    still serves the CLI and the dashboard chat.
    """

    @pytest.mark.parametrize("key", ["service", "federation", "standalone"])
    def test_ask_architecture_non_empty(self, named_projects: dict, key: str) -> None:
        from rag_search.query.ask import run_ask
        path = named_projects.get(key, "")
        assert path, f"{key} not in registry — all 3 project roles must be registered"
        result = run_ask("What is the overall architecture?", path, "architecture")
        assert isinstance(result, str) and len(result.strip()) > 100, (
            f"{key}: run_ask(architecture) returned empty/short: {result!r}"
        )


class TestNamedProjectsGraph:
    """T3d: graph tool works for each named root (at least definition relation)."""

    @pytest.mark.parametrize("key", ["service", "federation", "standalone"])
    def test_graph_definition_returns_dict(self, named_projects: dict, key: str) -> None:
        import sqlite3

        from rag_search.core.config import project_graph_db
        from rag_search.server.mcp import graph as graph_tool
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
