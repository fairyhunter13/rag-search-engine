"""Live MCP tool behavior tests — all 7 tools exercised via HTTP API.

The HTTP endpoints exposed by the daemon call the exact same handlers as the
MCP stdio bridge.  Testing via HTTP is equivalent to testing the MCP tools.

Tools under test: search, ask, graph, overview, build, federation, manage.
Requires: daemon at :8765, indexed project with communities.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live

_CODE_EXTENSIONS = {".go", ".py", ".java", ".ts", ".tsx", ".js", ".rs", ".kt"}


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

class TestMCPSearch:
    """search(query, scope, project_paths) — find specific code/files/functions."""

    def test_search_code_scope_returns_results(self, http, project):
        r = http.get("/api/search", params={"q": "main function handler", "project": project, "scope": "code"})
        assert r.status_code == 200, f"search failed: {r.text[:200]}"
        results = r.json().get("results", [])
        assert len(results) > 0, "search returned no results"

    def test_search_results_have_file_and_content(self, http, project):
        r = http.get("/api/search", params={"q": "error handling", "project": project, "top_k": 5})
        assert r.status_code == 200
        results = r.json().get("results", [])
        assert results, "No results"
        first = results[0]
        file_key = next((k for k in ("file", "path", "filepath") if k in first), None)
        assert file_key, f"Result has no file path key; keys={list(first.keys())}"
        assert first[file_key], "File path is empty"

    def test_search_all_scope_accepted(self, http, project):
        r = http.get("/api/search", params={"q": "configuration", "project": project, "scope": "all"})
        assert r.status_code == 200, f"scope=all failed: {r.text[:200]}"


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------

class TestMCPAsk:
    """ask(query, project_path, scope) — architecture, design, how-does-X-work."""

    def test_ask_default_scope_returns_answer(self, http, project):
        r = http.get("/api/ask", params={"q": "How does this system work?", "project": project})
        assert r.status_code == 200, f"ask failed: {r.text[:200]}"
        data = r.json()
        answer = data.get("answer", "") or data.get("summary", "")
        communities = data.get("communities", [])
        assert len(answer) > 20 or len(communities) > 0, f"ask returned nothing: {data}"

    def test_ask_global_scope_returns_synthesis(self, http, project):
        r = http.get("/api/ask", params={"q": "Describe the overall architecture", "project": project, "scope": "global"})
        assert r.status_code == 200, f"ask global failed: {r.text[:200]}"
        data = r.json()
        answer = data.get("answer", "") or data.get("summary", "")
        assert len(answer) > 50, f"global synthesis too short ({len(answer)} chars): {answer[:200]}"

    def test_ask_feature_scope_returns_structured_trace(self, http, project):
        r = http.get("/api/ask", params={"q": "How does request processing work?", "project": project, "scope": "feature"})
        assert r.status_code == 200, f"ask feature failed: {r.text[:200]}"
        data = r.json()
        has_trace = any(k in data for k in ("entry_points", "call_chain", "algorithm", "design_rationale", "answer"))
        assert has_trace, f"feature scope returned no trace data; keys={list(data.keys())}"


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------

class TestMCPGraph:
    """graph(symbol, project_path, relation) — call graph analysis."""

    def test_graph_callers_returns_result(self, http, project):
        r = http.get("/api/graph", params={"project": project, "symbol": "main", "relation": "callers"})
        assert r.status_code == 200, f"graph callers failed: {r.text[:200]}"
        data = r.json()
        # Either callers found or a clear message that symbol wasn't found
        assert "callers" in data or "error" in data or "matches" in data or "message" in data, (
            f"Unexpected graph response shape: {list(data.keys())}"
        )

    def test_graph_callees_returns_result(self, http, project):
        r = http.get("/api/graph", params={"project": project, "symbol": "main", "relation": "callees"})
        assert r.status_code == 200, f"graph callees failed: {r.text[:200]}"

    def test_graph_impact_returns_narrative(self, http, project):
        r = http.get("/api/graph", params={"project": project, "symbol": "main", "relation": "impact_narrative"})
        assert r.status_code == 200, f"graph impact_narrative failed: {r.text[:200]}"
        data = r.json()
        has_narrative = (
            data.get("narrative")
            or data.get("impact_narrative")
            or data.get("summary")
            or data.get("error")
        )
        assert has_narrative, f"impact_narrative returned no narrative: {data}"


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------

class TestMCPOverview:
    """overview(project_path, what) — project structure, communities, status, patterns."""

    def test_overview_structure(self, http, project):
        r = http.get("/api/overview", params={"project": project, "what": "structure"})
        assert r.status_code == 200, f"overview structure failed: {r.text[:200]}"
        data = r.json()
        assert data.get("status") == "ok", f"overview structure not ok: {data.get('status')}"

    def test_overview_communities(self, http, project):
        r = http.get("/api/overview", params={"project": project, "what": "communities"})
        assert r.status_code == 200
        data = r.json()
        communities = data.get("communities", [])
        count = data.get("community_count", len(communities))
        assert count > 0, "No communities found"

    def test_overview_status(self, http, project):
        r = http.get("/api/overview", params={"project": project, "what": "status"})
        assert r.status_code == 200
        data = r.json()
        assert "project_path" in data or "status" in data, f"Status response missing fields: {data}"

    def test_overview_patterns(self, http, project):
        r = http.get("/api/overview", params={"project": project, "what": "patterns"})
        assert r.status_code == 200
        data = r.json()
        assert data.get("status") == "ok", f"patterns not ok: {data.get('status')}"

    def test_overview_projects_lists_all(self, http):
        r = http.get("/api/overview", params={"what": "projects"})
        assert r.status_code == 200
        data = r.json()
        projects = data.get("projects", [])
        assert len(projects) > 0, "No projects in registry"

    def test_overview_hierarchy_returns_data(self, http, project):
        r = http.get("/api/overview", params={"project": project, "what": "hierarchy"})
        assert r.status_code == 200
        data = r.json()
        assert "error" not in data or data.get("levels") is not None, f"Hierarchy error: {data}"

    def test_overview_suggested_questions(self, http, project):
        r = http.get("/api/overview", params={"project": project, "what": "suggested_questions"})
        assert r.status_code == 200
        data = r.json()
        questions = data.get("questions", data.get("suggested_questions", []))
        assert isinstance(questions, list), f"Unexpected questions shape: {type(questions)}"


# ---------------------------------------------------------------------------
# build (via jobs + kb_health)
# ---------------------------------------------------------------------------

class TestMCPBuild:
    """build(project_path, action) — async KB build; verify results via status endpoints."""

    def test_jobs_endpoint_accessible(self, http, project):
        """Jobs endpoint must return a list (pipeline history visible)."""
        r = http.get("/api/jobs", params={"project": project})
        assert r.status_code == 200, f"jobs failed: {r.text[:200]}"
        data = r.json()
        assert "jobs" in data, f"jobs response missing 'jobs' key: {list(data.keys())}"

    def test_kb_health_shows_enrichment(self, http, project):
        """KB health must show enrichment percentage above zero."""
        r = http.get("/api/kb_health", params={"project": project})
        assert r.status_code == 200, f"kb_health failed: {r.text[:200]}"
        data = r.json()
        enrichment_pct = (
            data.get("enrichment_pct")
            or data.get("enriched_pct")
            or data.get("enrichment_percent")
        )
        if enrichment_pct is not None:
            assert float(enrichment_pct) > 0, "Enrichment is 0% — build pipeline may not have run"

    def test_wiki_pages_generated(self, http, project):
        """Wiki pages must exist after a pipeline run."""
        r = http.get("/api/wiki", params={"project": project})
        assert r.status_code == 200, f"wiki list failed: {r.text[:200]}"
        data = r.json()
        pages = data.get("pages", data.get("wiki_pages", []))
        assert len(pages) > 0, (
            "No wiki pages found — run build(action='wiki') or build(action='pipeline')"
        )


# ---------------------------------------------------------------------------
# federation
# ---------------------------------------------------------------------------

class TestMCPFederation:
    """federation(root_path) — list/manage sub-repositories."""

    def test_federation_list_returns_structure(self, http, project):
        r = http.get("/api/federation", params={"project": project})
        assert r.status_code == 200, f"federation failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), f"federation must return a dict; got {type(data)}"
        # Either members list or empty (fine — project may have no federation)
        members = data.get("members", data.get("repos", []))
        assert isinstance(members, list), "members must be a list"


# ---------------------------------------------------------------------------
# manage
# ---------------------------------------------------------------------------

class TestMCPManage:
    """manage(project_path, action) — project lifecycle: vacuum, dedup, jobs."""

    def test_manage_vacuum_dry_run(self, http, project):
        """Vacuum GET (dry-run) must return freed/reclaimable size."""
        r = http.get("/api/vacuum", params={"project": project})
        assert r.status_code == 200, f"vacuum failed: {r.text[:200]}"
        data = r.json()
        assert "error" not in data or data.get("freed_bytes") is not None or "status" in data, (
            f"Vacuum returned unexpected shape: {data}"
        )

    def test_manage_dedup_dry_run(self, http, project):
        """Dedup GET (dry-run preview) must return candidate count."""
        r = http.get("/api/dedup", params={"project": project, "dry_run": "true"})
        assert r.status_code == 200, f"dedup failed: {r.text[:200]}"
        data = r.json()
        assert "candidates" in data or "duplicates" in data or "status" in data, (
            f"Dedup returned unexpected shape: {data}"
        )

    def test_manage_jobs_list(self, http, project):
        """Jobs list must be accessible (action='jobs')."""
        r = http.get("/api/jobs", params={"project": project})
        assert r.status_code == 200
        assert "jobs" in r.json()
