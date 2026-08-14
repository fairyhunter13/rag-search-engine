"""MCP tool matrix — all 4 tools × all variants not already in test_server.

Covers (no duplication of test_p5 or test_p21):
  - graph: all 6 relations (definition/callers/callees/impact/impact_narrative/path)
  - search: 3 scopes (code/docs/all) + federated project_paths
  - overview: metrics / projects (the what= values that fail on a stale index; `patterns` was
    the third and left with tier 3)

`ask` was the fifth tool and its scope matrix stood here. It is off the MCP surface — the
scopes live on for the CLI and the dashboard's chat, and are exercised by
test_retrieval_routing (RR1-RR8) against compose_answer directly, which is where they are
actually decided.

Requires daemon at :8765 with ≥1 indexed project.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from rag_search.core.config import project_graph_db
from rag_search.query.search import SCOPES
from tests.live._sample_workspace import SampleWorkspace

pytestmark = pytest.mark.live

_GRAPH_RELATIONS_SIMPLE = ["definition", "callers", "callees", "impact", "impact_narrative"]


@pytest.fixture(scope="module")
def indexed_proj(sample_workspace: SampleWorkspace) -> str:
    """Sample promo-svc — has vectors.db (GPU-indexed by sample_workspace fixture)."""
    return sample_workspace.promo


@pytest.fixture(scope="module")
def graph_proj(sample_workspace: SampleWorkspace) -> str:
    """Sample promo-svc — has graph.db with symbols (GPU-indexed by sample_workspace fixture)."""
    return sample_workspace.promo


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
        from rag_search.server.mcp import graph as graph_tool
        result = asyncio.run(graph_tool(any_symbol, graph_proj, relation))
        data = json.loads(result)
        assert isinstance(data, dict), f"graph({relation!r}) must return JSON object"

    def test_graph_path_without_to_symbol_returns_error_or_empty(self, graph_proj, any_symbol):
        from rag_search.server.mcp import graph as graph_tool
        result = asyncio.run(graph_tool(any_symbol, graph_proj, "path"))
        data = json.loads(result)
        assert "error" in data or "path" in data

    def test_graph_unknown_relation_errors_with_valid_set(self, graph_proj, any_symbol):
        """An unadvertised relation must error, not answer.

        `semantic_trace` outlived its implementation here for exactly one reason: the old
        fallthrough answered it with `path` semantics, and the test this replaces asserted only
        that the result was a dict — true of every branch, so it never discriminated.
        """
        from rag_search.server.mcp import graph as graph_tool
        data = json.loads(asyncio.run(graph_tool(any_symbol, graph_proj, "semantic_trace")))
        assert "error" in data, f"unknown relation must error; got {data}"
        assert "semantic_trace" not in data.get("valid", []), (
            "semantic_trace must not be advertised as valid — it has no implementation"
        )

    def test_graph_nonexistent_returns_error(self):
        from rag_search.server.mcp import graph as graph_tool
        data = json.loads(asyncio.run(graph_tool("foo", "/nonexistent", "definition")))
        assert "error" in data


class TestSearchScopes:

    @pytest.mark.parametrize("scope", SCOPES)
    def test_search_scope(self, indexed_proj, scope):
        from rag_search.server.mcp import search as search_tool
        data = json.loads(asyncio.run(
            search_tool("function", scope=scope, project_paths=[indexed_proj])
        ))
        assert "total" in data and "results" in data, f"search(scope={scope!r}) missing keys"
        assert isinstance(data["results"], list)

    def test_search_unknown_scope_errors_with_valid_set(self, indexed_proj):
        """An unknown scope must error, not silently widen the corpus.

        This is `test_graph_unknown_relation_errors_with_valid_set` for the one tool of the three
        that did not have it. `scope_languages` answers "no restriction" and "I don't recognise
        that scope" with the same `None`, so the fallthrough searched *more* than was asked for
        while looking like a normal result — undetectable from the results, unlike a narrowing.

        The case is a project path rather than a typo because that is the one that actually
        happened: `scope=<path>` with the project selector left defaulted was accepted and
        answered from a different project entirely, which reads as a scope-routing defect in the
        engine rather than a bad argument. Asserting the valid set and the `project_paths` hint
        are both present is what makes the reply self-correcting.
        """
        from rag_search.server.mcp import search as search_tool
        data = json.loads(asyncio.run(
            search_tool("function", scope=indexed_proj, project_paths=[indexed_proj])
        ))
        assert "error" in data, f"unknown scope must error, not answer; got {data}"
        assert sorted(data.get("valid", [])) == sorted(SCOPES), (
            f"the error must advertise the valid scopes; got {data.get('valid')}")
        assert "project_paths" in data.get("hint", ""), (
            "the hint must name the parameter that actually selects projects, since confusing "
            f"the two is the failure this catches; got {data.get('hint')!r}")
        assert "results" not in data, "an unknown scope must not also return results"

    def test_search_federated_project_paths(self, indexed_proj):
        from rag_search.server.mcp import search as search_tool
        data = json.loads(asyncio.run(
            search_tool("function", project_paths=[indexed_proj])
        ))
        assert indexed_proj in data.get("projects_searched", [])


class TestOverviewNewWhats:

    @pytest.mark.parametrize("what", ["metrics", "projects"])
    def test_overview_what_returns_nonempty_dict(self, indexed_proj, what):
        from rag_search.server.mcp import overview as overview_tool
        data = json.loads(asyncio.run(overview_tool(indexed_proj, what)))
        assert isinstance(data, dict), f"overview(what={what!r}) must return JSON object"
        assert data, f"overview(what={what!r}) must not return empty dict"

    # `isinstance(data, dict) and data` above is a liveness check, not an existence one:
    # handle_overview rejects an unknown `what` with {"error": ...}, which is itself a non-empty
    # dict. Whether a variant exists is asserted by test_feature_proof.py::test_fp1, which
    # requires the word "unknown" in that error.
