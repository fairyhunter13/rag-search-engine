"""MCP tool validation tests against a real indexed project (redacted-name-3).

Skipped unless OPENCODE_RUN_LARGE_TESTS=1 and a running daemon.
These tests call the handlers directly (no MCP protocol overhead).
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

_LARGE = pytest.mark.large
_ASTRO = os.environ.get(
    "OPENCODE_TEST_PROJECT",
    "/home/user/git/github.com/fairyhunter13/redacted-name-3",
)


def _use_real_registry(monkeypatch):
    real = Path.home() / ".local" / "share" / "opencode-search" / "projects.json"
    monkeypatch.setenv("OPENCODE_REGISTRY_PATH", str(real))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Tool 1: search
# ---------------------------------------------------------------------------

@_LARGE
class TestSearchTool:
    def test_search_code_returns_results(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_search_code
        r = _run(handle_search_code("authentication", project_paths=[_ASTRO], top_k=5))
        assert isinstance(r, dict)
        assert "results" in r, f"Expected 'results' key, got: {list(r.keys())}"
        assert isinstance(r["results"], list)

    def test_search_results_have_path_and_score(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_search_code
        r = _run(handle_search_code("main handler", project_paths=[_ASTRO], top_k=3))
        for result in r.get("results", []):
            assert "path" in result, f"Result missing 'path': {result}"
            assert "score" in result, f"Result missing 'score': {result}"
            score = result["score"]
            assert 0.0 <= score <= 1.5, f"Score out of range: {score}"

    def test_search_docs_scope(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_search_code
        r = _run(handle_search_code(
            "authentication", project_paths=[_ASTRO], top_k=5, scope="docs"
        ))
        assert isinstance(r, dict)
        assert "results" in r or "error" not in r


# ---------------------------------------------------------------------------
# Tool 2: ask
# ---------------------------------------------------------------------------

@_LARGE
class TestAskTool:
    def test_wiki_query_returns_nonempty(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_wiki_query
        r = _run(handle_wiki_query("how does authentication work", project_path=_ASTRO, top_k=3))
        assert isinstance(r, dict)
        answer = r.get("answer") or r.get("results") or r.get("pages") or r.get("content")
        assert answer is not None, f"Expected non-None answer, got keys: {list(r.keys())}"
        assert "Error" not in str(answer)

    def test_global_search_returns_dict(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_global_search
        r = _run(handle_global_search("main architecture", project_path=_ASTRO, top_k=3))
        assert isinstance(r, dict)
        assert "error" not in r


# ---------------------------------------------------------------------------
# Tool 3: graph
# ---------------------------------------------------------------------------

@_LARGE
class TestGraphTool:
    def _symbol(self) -> str:
        import sqlite3

        from opencode_search.config import get_project_graph_db_path
        db = get_project_graph_db_path(_ASTRO)
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT name FROM nodes WHERE kind IN ('function','method') AND name != '' LIMIT 1"
        ).fetchone()
        conn.close()
        return row[0] if row else "main"

    def test_get_symbol_definition(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_get_symbol
        r = _run(handle_get_symbol(self._symbol(), _ASTRO))
        assert isinstance(r, dict)

    def test_get_callers_returns_dict(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_get_callers
        r = _run(handle_get_callers(self._symbol(), _ASTRO, depth=2))
        assert isinstance(r, dict)
        assert "error" not in r

    def test_get_callees_returns_dict(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_get_callees
        r = _run(handle_get_callees(self._symbol(), _ASTRO, depth=2))
        assert isinstance(r, dict)
        assert "error" not in r

    def test_detect_impact_returns_dict(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_detect_impact
        r = _run(handle_detect_impact(self._symbol(), _ASTRO))
        assert isinstance(r, dict)
        assert "error" not in r

    def test_trace_path_same_symbol(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_trace_path
        sym = self._symbol()
        r = _run(handle_trace_path(sym, sym, _ASTRO))
        assert isinstance(r, dict)


# ---------------------------------------------------------------------------
# Tool 4: overview
# ---------------------------------------------------------------------------

@_LARGE
class TestOverviewTool:
    def test_project_structure_returns_dict(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_project_structure
        r = _run(handle_project_structure(_ASTRO, max_depth=2))
        assert isinstance(r, dict)
        assert "error" not in r

    def test_get_communities_nonempty(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_get_communities
        r = _run(handle_get_communities(project_path=_ASTRO, top_k=10))
        assert isinstance(r, dict)
        comms = r.get("communities", [])
        assert len(comms) >= 1, "Expected at least 1 community"

    def test_detect_patterns_has_architecture(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_detect_patterns
        r = _run(handle_detect_patterns(project_path=_ASTRO))
        assert isinstance(r, dict)
        assert r.get("status") == "ok", f"Expected status=ok, got {r.get('status')}"
        has_arch = "architecture" in r or "llm_analysis" in r
        assert has_arch, f"Expected architecture data, got keys: {list(r.keys())}"

    def test_list_indexed_projects_nonempty(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_list_indexed_projects
        r = _run(handle_list_indexed_projects())
        assert isinstance(r, dict)
        projects = r.get("projects", [])
        assert len(projects) >= 1


# ---------------------------------------------------------------------------
# Tool 5: build (graph export as KB check)
# ---------------------------------------------------------------------------

@_LARGE
class TestBuildTool:
    def test_graph_export_kb_exists(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_graph_export
        r = _run(handle_graph_export(project_path=_ASTRO, format="json", max_nodes=50))
        assert isinstance(r, dict)
        assert "nodes" in r and "edges" in r


# ---------------------------------------------------------------------------
# Tool 6: federation
# ---------------------------------------------------------------------------

@_LARGE
class TestFederationTool:
    def test_list_federation_returns_dict(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_list_federation
        r = _run(handle_list_federation(project_path=_ASTRO))
        assert isinstance(r, dict)
        assert "error" not in r


# ---------------------------------------------------------------------------
# Tool 7: manage
# ---------------------------------------------------------------------------

@_LARGE
class TestManageTool:
    def test_project_status_returns_dict(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_project_status
        r = _run(handle_project_status(path=_ASTRO))
        assert isinstance(r, dict)
        assert "error" not in r


# ---------------------------------------------------------------------------
# All 7 tools — no-exception property
# ---------------------------------------------------------------------------

@_LARGE
class TestAllToolsNeverCrash:
    def test_all_tools_return_dict(self, monkeypatch):
        """Every tool must return a dict, never raise an uncaught exception."""
        _use_real_registry(monkeypatch)
        import sqlite3

        from opencode_search.config import get_project_graph_db_path
        from opencode_search.handlers import (
            handle_detect_impact,
            handle_detect_patterns,
            handle_get_callees,
            handle_get_callers,
            handle_get_communities,
            handle_get_symbol,
            handle_global_search,
            handle_graph_export,
            handle_list_federation,
            handle_list_indexed_projects,
            handle_project_status,
            handle_project_structure,
            handle_search_code,
            handle_trace_path,
            handle_wiki_query,
        )
        db = get_project_graph_db_path(_ASTRO)
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT name FROM nodes WHERE kind='function' AND name != '' LIMIT 1"
        ).fetchone()
        conn.close()
        sym = row[0] if row else "main"

        calls = [
            handle_search_code("test", project_paths=[_ASTRO], top_k=3),
            handle_wiki_query("architecture", project_path=_ASTRO, top_k=2),
            handle_global_search("design", project_path=_ASTRO, top_k=2),
            handle_get_symbol(sym, _ASTRO),
            handle_get_callers(sym, _ASTRO, depth=1),
            handle_get_callees(sym, _ASTRO, depth=1),
            handle_detect_impact(sym, _ASTRO),
            handle_trace_path(sym, sym, _ASTRO),
            handle_graph_export(project_path=_ASTRO, format="json", max_nodes=20),
            handle_detect_patterns(project_path=_ASTRO),
            handle_get_communities(project_path=_ASTRO, top_k=5),
            handle_project_structure(_ASTRO, max_depth=2),
            handle_list_indexed_projects(),
            handle_list_federation(project_path=_ASTRO),
            handle_project_status(path=_ASTRO),
        ]
        for coro in calls:
            result = _run(coro)
            assert isinstance(result, dict), f"Handler returned non-dict: {type(result)}"
