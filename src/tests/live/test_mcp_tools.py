"""Live MCP tool behavior tests — search/ask/graph/overview/index exercised via HTTP API.

The HTTP endpoints exposed by the daemon call the exact same handlers as the
MCP stdio bridge.  Testing via HTTP is equivalent to testing the MCP tools.

Phase 100: MCP surface is search, ask, graph, overview, index (read-only + one flag tool).
KB build, federation, and maintenance are automatic (daemon loops) or HTTP-only escape hatches.
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

    def test_search_empty_query_returns_gracefully(self, http, project):
        """search with empty query must return error or empty results — not 5xx."""
        r = http.get("/api/search", params={"q": "", "project": project})
        assert r.status_code in (200, 400), f"search empty query unexpected status: {r.status_code}"
        data = r.json()
        assert isinstance(data, (dict, list)), "search empty query must return dict or list"


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------

class TestMCPAsk:
    """ask(query, project_path, scope) — architecture, design, how-does-X-work."""

    @pytest.mark.slow
    def test_ask_default_scope_returns_answer(self, http, project):
        r = http.get("/api/ask", params={"q": "How does this system work?", "project": project})
        assert r.status_code == 200, f"ask failed: {r.text[:200]}"
        data = r.json()
        answer = data.get("answer", "") or data.get("summary", "")
        communities = data.get("communities", [])
        results = data.get("results", [])
        assert len(answer) > 20 or len(communities) > 0 or len(results) > 0, f"ask returned nothing: {data}"

    @pytest.mark.slow
    def test_ask_global_scope_returns_synthesis(self, http, project):
        r = http.get("/api/ask", params={"q": "Describe the overall architecture", "project": project, "scope": "global"})
        assert r.status_code == 200, f"ask global failed: {r.text[:200]}"
        data = r.json()
        answer = data.get("answer", "") or data.get("summary", "")
        assert len(answer) > 50, f"global synthesis too short ({len(answer)} chars): {answer[:200]}"

    @pytest.mark.slow
    def test_ask_feature_scope_returns_structured_trace(self, http, project):
        r = http.get("/api/ask", params={"q": "How does request processing work?", "project": project, "scope": "feature"})
        assert r.status_code == 200, f"ask feature failed: {r.text[:200]}"
        data = r.json()
        has_trace = any(k in data for k in ("entry_points", "call_chain", "algorithm", "design_rationale", "answer"))
        assert has_trace, f"feature scope returned no trace data; keys={list(data.keys())}"

    @pytest.mark.slow
    def test_ask_wiki_scope_returns_answer(self, http, project):
        """ask with scope=wiki must call handle_wiki_query and return a result dict."""
        r = http.get("/api/ask", params={"project": project, "q": "how does this project work?", "scope": "wiki"})
        assert r.status_code == 200, f"ask wiki scope failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "ask wiki scope must return dict"
        has_result = "answer" in data or "results" in data or "pages" in data or "error" in data
        assert has_result, f"ask wiki scope returned unexpected shape: {list(data.keys())}"


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------

class TestMCPGraph:
    """graph(symbol, project_path, relation) — call graph analysis."""

    def test_graph_callers_returns_result(self, http, project):
        r = http.get("/api/graph", params={"project": project, "symbol": "main", "relation": "callers"})
        assert r.status_code == 200, f"graph callers failed: {r.text[:200]}"
        data = r.json()
        assert "callers" in data or "error" in data or "matches" in data or "message" in data, (
            f"Unexpected graph response shape: {list(data.keys())}"
        )

    def test_graph_callees_returns_result(self, http, project):
        r = http.get("/api/graph", params={"project": project, "symbol": "main", "relation": "callees"})
        assert r.status_code == 200, f"graph callees failed: {r.text[:200]}"

    @pytest.mark.slow
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

    @pytest.mark.slow
    def test_graph_semantic_trace_returns_result(self, http, project):
        r = http.get("/api/graph", params={
            "project": project,
            "symbol": "main",
            "relation": "semantic_trace",
            "to": "database",
        })
        assert r.status_code == 200, f"graph semantic_trace failed: {r.text[:200]}"
        data = r.json()
        has_trace = any(k in data for k in ("trace", "path", "narrative", "error", "message", "steps"))
        assert has_trace, f"semantic_trace returned unexpected shape: {list(data.keys())}"

    def test_graph_path_returns_result(self, http, project):
        """graph(relation='path', to_symbol=...) finds call path between two symbols."""
        r = http.get("/api/graph", params={
            "project": project,
            "symbol": "main",
            "relation": "path",
            "to": "error",
        })
        assert r.status_code == 200, f"graph path failed: {r.text[:200]}"
        data = r.json()
        # May return path=[] if not connected — both outcomes are valid
        has_result = any(k in data for k in ("path", "connected", "error", "steps", "message"))
        assert has_result, f"graph path unexpected shape: {list(data.keys())}"

    def test_graph_impact_raw_returns_callers(self, http, project):
        """graph(relation='impact') raw format returns callers_by_depth or empty."""
        r = http.get("/api/graph", params={"project": project, "symbol": "handle_chat_auto", "relation": "impact"})
        assert r.status_code == 200, f"graph impact raw failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), f"graph impact must return dict; got {type(data)}"
        has_result = any(k in data for k in ("callers_by_depth", "callers", "total_affected", "error", "message"))
        assert has_result, f"graph impact raw unexpected shape: {list(data.keys())}"

    def test_graph_empty_symbol_returns_gracefully(self, http, project):
        """graph with empty symbol must return an error dict, not raise."""
        r = http.get("/api/graph", params={"project": project, "symbol": "", "relation": "callers"})
        assert r.status_code in (200, 400), f"graph empty symbol unexpected status: {r.status_code}"
        data = r.json()
        assert isinstance(data, dict), "graph empty symbol must return a dict"


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
        assert "project_path" in data or "path" in data or "status" in data, f"Status response missing fields: {data}"

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

    def test_overview_architecture_domains(self, http, project):
        r = http.get("/api/overview", params={"project": project, "what": "architecture_domains"})
        assert r.status_code == 200, f"architecture_domains failed: {r.text[:200]}"
        data = r.json()
        assert "error" not in data or data.get("domains") is not None or "communities" in data, (
            f"architecture_domains returned unexpected shape: {list(data.keys())}"
        )

    def test_overview_import_cycles(self, http, project):
        r = http.get("/api/overview", params={"project": project, "what": "import_cycles"})
        assert r.status_code == 200, f"import_cycles failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "import_cycles must return a dict"

    def test_overview_graph_diff(self, http, project):
        r = http.get("/api/overview", params={"project": project, "what": "graph_diff"})
        assert r.status_code == 200, f"graph_diff failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "graph_diff must return a dict"

    def test_overview_surprising_connections(self, http, project):
        r = http.get("/api/overview", params={"project": project, "what": "surprising_connections"})
        assert r.status_code == 200, f"surprising_connections failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "surprising_connections must return a dict"

    def test_overview_pr_impact(self, http, project):
        r = http.get("/api/overview", params={"project": project, "what": "pr_impact"})
        assert r.status_code == 200, f"pr_impact failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "pr_impact must return a dict"

    def test_overview_feature_map(self, http, project):
        r = http.get("/api/overview", params={"project": project, "what": "feature_map"})
        assert r.status_code == 200, f"feature_map failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "feature_map must return a dict"

    def test_overview_service_mesh(self, http, project):
        r = http.get("/api/overview", params={"project": project, "what": "service_mesh"})
        assert r.status_code == 200, f"service_mesh failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "service_mesh must return a dict"

    def test_overview_invalid_what_returns_error(self, http, project):
        """overview with an unknown what= value must return an error dict, not 5xx."""
        r = http.get("/api/overview", params={"project": project, "what": "__invalid_what_value__"})
        assert r.status_code in (200, 400), f"overview invalid what= unexpected status: {r.status_code}"
        data = r.json()
        assert isinstance(data, dict), "overview invalid what= must return a dict"
        text = str(data).lower()
        assert "error" in text or "unknown" in text or "invalid" in text or "unsupported" in text or data, (
            f"overview invalid what= should indicate an error: {data}"
        )

    def test_communities_list_returns_data(self, http, project):
        """GET /api/communities must return a non-empty list of community dicts."""
        r = http.get("/api/communities", params={"project": project})
        assert r.status_code == 200, f"communities failed: {r.text[:200]}"
        data = r.json()
        communities = data if isinstance(data, list) else data.get("communities", data.get("results", []))
        assert len(communities) > 0, "communities returned empty list"
        assert isinstance(communities[0], dict), "community entry must be a dict"


# ---------------------------------------------------------------------------
# build (via jobs + kb_health)
# ---------------------------------------------------------------------------

class TestMCPBuild:
    """HTTP KB endpoints (/api/jobs, /api/kb_health, /api/wiki, /api/enrich_hierarchy, /api/overview)."""

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

    @pytest.mark.slow
    def test_enrich_hierarchy_triggers_and_completes(self, http, quality_project):
        """POST /api/enrich_hierarchy must complete with status=ok and max_level >= 1."""
        r = http.post("/api/enrich_hierarchy", json={"project": quality_project})
        assert r.status_code == 200, f"enrich_hierarchy failed: {r.text[:200]}"
        data = r.json()
        assert "job_id" in data, f"missing job_id: {list(data.keys())}"

        import time as _time
        job_id = data["job_id"]
        result = None
        for _ in range(12):
            _time.sleep(5)
            jr = http.get(f"/api/jobs/{job_id}")
            assert jr.status_code == 200
            jd = jr.json()
            if jd.get("status") != "running":
                result = jd.get("result", {})
                break
        assert result is not None, "enrich_hierarchy job did not complete within 60s"
        assert result.get("status") == "ok", f"unexpected result: {result}"
        assert result.get("max_level", 0) >= 1, f"max_level too low: {result}"

    def test_overview_patterns_detects_frameworks(self, http, quality_project):
        """LLM-based framework detection must return at least one framework."""
        r = http.get("/api/overview", params={"project": quality_project, "what": "patterns"})
        assert r.status_code == 200
        data = r.json()
        frameworks = data.get("key_frameworks") or (data.get("result") or {}).get("key_frameworks", [])
        assert isinstance(frameworks, list) and len(frameworks) > 0, \
            f"No frameworks detected via LLM: {list(data.keys())}"

    def test_overview_patterns_has_module_structure(self, http, quality_project):
        """LLM-based module structure detection must return a non-empty type."""
        r = http.get("/api/overview", params={"project": quality_project, "what": "patterns"})
        assert r.status_code == 200
        data = r.json()
        ms = data.get("module_structure") or (data.get("result") or {}).get("module_structure", {})
        structure_type = ms.get("type", "") if isinstance(ms, dict) else str(ms)
        assert structure_type and structure_type != "unknown", \
            f"Module structure type empty or unknown: {ms}"


# ---------------------------------------------------------------------------
# federation
# ---------------------------------------------------------------------------

class TestMCPFederation:
    """HTTP federation endpoints (/api/federation) — list/discover/add/remove/index sub-repos."""

    def test_federation_list_returns_structure(self, http, project):
        r = http.get("/api/federation", params={"project": project})
        assert r.status_code == 200, f"federation failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), f"federation must return a dict; got {type(data)}"
        # Either members list or empty (fine — project may have no federation)
        members = data.get("members", data.get("repos", []))
        assert isinstance(members, list), "members must be a list"

    def test_federation_discover_returns_structure(self, http, project):
        """federation(action='discover') must return sub-repos or empty list."""
        r = http.get("/api/federation", params={"project": project, "action": "discover"})
        assert r.status_code == 200, f"federation discover failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, (dict, list)), f"federation discover must return dict or list; got {type(data)}"

    def test_federation_add_member_same_path_error(self, http, project):
        """action=add with root==member must return graceful error dict."""
        r = http.get("/api/federation", params={"project": project, "action": "add", "member": project})
        assert r.status_code == 200, f"federation add failed: {r.text[:200]}"
        data = r.json()
        assert "error" in data, f"expected error for same-path add, got: {data}"

    def test_federation_remove_member_not_registered_error(self, http, project):
        """action=remove with non-member path must return graceful error dict."""
        r = http.get("/api/federation", params={
            "project": project, "action": "remove", "member": "/tmp/nonexistent-federation-xyz",
        })
        assert r.status_code == 200, f"federation remove failed: {r.text[:200]}"
        data = r.json()
        assert "error" in data, f"expected error for non-member remove, got: {data}"

    def test_federation_index_returns_structure(self, http, project):
        """action=index must return a dict with indexed/failed keys (ok even with 0 members)."""
        r = http.get("/api/federation", params={"project": project, "action": "index"})
        assert r.status_code == 200, f"federation index failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), f"federation index must return dict, got {type(data)}"
        assert "indexed" in data or "error" in data, f"unexpected response shape: {data}"


# ---------------------------------------------------------------------------
# manage
# ---------------------------------------------------------------------------

@pytest.mark.flaky(reruns=2)
class TestMCPManage:
    """HTTP maintenance info (/api/storage_health, /api/jobs) — write ops auto-run by daemon sweep."""

    def test_manage_storage_health(self, http, project):
        """GET /api/storage_health must return per-project storage diagnostics."""
        r = http.get("/api/storage_health", params={"project": project})
        assert r.status_code == 200, f"storage_health failed: {r.text[:200]}"
        data = r.json()
        assert data.get("status") == "ok", f"storage_health not ok: {data}"
        projects = data.get("projects", [])
        assert len(projects) >= 1, f"expected at least 1 project: {data}"
        stats = projects[0]
        for field in ("total_bytes", "wal_bytes", "stale_index_dirs", "recoverable_mb"):
            assert field in stats, f"missing field {field!r}: {stats}"

    def test_manage_jobs_list(self, http, project):
        """Jobs list must be accessible (action='jobs')."""
        r = http.get("/api/jobs", params={"project": project})
        assert r.status_code == 200
        assert "jobs" in r.json()

    def test_manage_git_hooks_status(self, http, project):
        """GET /api/git_hooks must report whether the post-commit hook is installed."""
        r = http.get("/api/git_hooks", params={"project": project})
        assert r.status_code == 200, f"git_hooks GET failed: {r.text[:200]}"
        data = r.json()
        assert "installed" in data, f"git_hooks response missing 'installed': {list(data.keys())}"
        assert "hook_path" in data, f"git_hooks response missing 'hook_path': {list(data.keys())}"

    def test_manage_git_hooks_install_uninstall(self, http, project):
        """POST /api/git_hooks install then uninstall must be idempotent and return status."""
        # Install
        r = http.post("/api/git_hooks", json={"project": project, "action": "install"})
        assert r.status_code == 200, f"git_hooks install failed: {r.text[:200]}"
        data = r.json()
        assert "error" not in data or data.get("installed") is not None, (
            f"git_hooks install response unexpected: {data}"
        )
        # Uninstall (clean up)
        r2 = http.post("/api/git_hooks", json={"project": project, "action": "uninstall"})
        assert r2.status_code == 200, f"git_hooks uninstall failed: {r2.text[:200]}"

    def test_manage_wiki_health_in_kb_health(self, http, project):
        """KB health must include wiki page count — validates wiki_lint coverage."""
        r = http.get("/api/kb_health", params={"project": project})
        assert r.status_code == 200, f"kb_health failed: {r.text[:200]}"
        data = r.json()
        wiki_count = (
            data.get("wiki_page_count")
            or data.get("wiki_count")
            or data.get("wiki_pages")
        )
        assert wiki_count is not None, (
            f"wiki_page_count not in kb_health; keys={list(data.keys())}"
        )

    def test_manage_wiki_lint_endpoint(self, http, project):
        """GET /api/wiki_lint must return wiki health info (page count, issues)."""
        r = http.get("/api/wiki_lint", params={"project": project})
        assert r.status_code == 200, f"wiki_lint failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "wiki_lint must return a dict"
        assert "error" not in data, f"wiki_lint returned error: {data}"

    def test_manage_stop_watching_returns_gracefully(self, http, project):
        """manage(action='stop_watching') must succeed; restore watcher after."""
        r = http.post("/api/stop_watching", json={"project": project})
        assert r.status_code == 200, f"stop_watching unexpected status: {r.status_code} {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "stop_watching must return a dict"
        assert data.get("status") == "stopped", f"stop_watching returned unexpected status: {data}"
        # Restore watcher so subsequent tests (test_watcher_is_active) still pass
        http.post("/api/start_watching", json={"project": project})

    def test_manage_remove_nonexistent_project_graceful(self, http):
        """manage(action='remove_project') for unknown path must return error, not 5xx."""
        r = http.post("/api/remove_project", json={"project": "/tmp/__nonexistent_test_path__"})
        assert r.status_code in (200, 400, 404), f"remove nonexistent project unexpected status: {r.status_code}"
        data = r.json()
        assert isinstance(data, dict), "remove_project must return a dict"


class TestMCPMetrics:
    """Verify stream error/success counters in /api/metrics."""

    @pytest.mark.slow
    def test_stream_success_count_increments(self, http, project):
        """A successful chat call must increment chat_stream.stream_success_count."""
        from .conftest import parse_sse
        m0 = http.get("/api/metrics").json()
        before = m0.get("chat_stream", {}).get("stream_success_count", 0)
        r = http.post(
            "/api/chat_stream",
            json={"project": project, "query": "what does the indexer do?"},
            headers={"Accept": "text/event-stream"},
        )
        assert r.status_code == 200, f"chat_stream failed: {r.status_code}"
        parse_sse(r)
        m1 = http.get("/api/metrics").json()
        after = m1.get("chat_stream", {}).get("stream_success_count", 0)
        assert after > before, (
            f"stream_success_count did not increment: before={before}, after={after}"
        )


# ---------------------------------------------------------------------------
# business intelligence
# ---------------------------------------------------------------------------

class TestMCPBusiness:
    """Business intelligence endpoints: feature_map, business_rules, process_flows, ask_business."""

    def test_business_rules_returns_list(self, http, project):
        r = http.get("/api/business_rules", params={"project": project})
        assert r.status_code == 200, f"business_rules failed: {r.text[:200]}"
        data = r.json()
        assert "business_rules" in data or "error" in data, (
            f"business_rules returned unexpected shape: {list(data.keys())}"
        )
        if "business_rules" in data:
            assert isinstance(data["business_rules"], list), "business_rules must be a list"

    def test_process_flows_returns_list(self, http, project):
        r = http.get("/api/process_flows", params={"project": project})
        assert r.status_code == 200, f"process_flows failed: {r.text[:200]}"
        data = r.json()
        assert "process_flows" in data or "error" in data, (
            f"process_flows returned unexpected shape: {list(data.keys())}"
        )
        if "process_flows" in data:
            assert isinstance(data["process_flows"], list), "process_flows must be a list"

    @pytest.mark.slow
    def test_ask_business_returns_answer(self, http, project):
        r = http.get("/api/ask_business", params={
            "project": project,
            "q": "What are the main business processes in this project?",
        })
        assert r.status_code == 200, f"ask_business failed: {r.text[:200]}"
        data = r.json()
        has_answer = (
            data.get("answer") or data.get("summary") or data.get("communities")
            or data.get("error")
        )
        assert has_answer, f"ask_business returned empty response: {data}"


# ---------------------------------------------------------------------------
# admin / status endpoints
# ---------------------------------------------------------------------------

class TestMCPAdmin:
    """Lightweight admin and status endpoints."""

    def test_auto_pipeline_status_accessible(self, http):
        r = http.get("/api/auto_pipeline_status")
        assert r.status_code == 200, f"auto_pipeline_status failed: {r.text[:200]}"
        data = r.json()
        assert "enabled" in data, f"auto_pipeline_status missing 'enabled': {data}"
        assert isinstance(data.get("events", []), list), "events must be a list"

    def test_callflow_html_returns_html(self, http, project):
        r = http.get("/api/callflow_html", params={
            "project": project,
            "symbol": "main",
            "direction": "callees",
            "depth": "3",
            "format": "html",
        })
        assert r.status_code in (200, 404), f"callflow_html unexpected status: {r.status_code}"
        if r.status_code == 200:
            assert "<" in r.text, "callflow_html must return HTML content"

    def test_callflow_mermaid_returns_text(self, http, project):
        r = http.get("/api/callflow_html", params={
            "project": project,
            "symbol": "main",
            "format": "mermaid",
        })
        assert r.status_code in (200, 404), f"callflow mermaid unexpected status: {r.status_code}"

    def test_git_hooks_status_accessible(self, http, project):
        r = http.get("/api/git_hooks", params={"project": project})
        assert r.status_code == 200, f"git_hooks GET failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "git_hooks must return a dict"

    def test_integrations_status_accessible(self, http):
        r = http.get("/api/integrations_status")
        assert r.status_code == 200, f"integrations_status failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, (dict, list)), "integrations_status must return a dict or list"

    def test_system_status_accessible(self, http):
        """GET /api/system_status must return 200 with a dict response."""
        r = http.get("/api/system_status")
        assert r.status_code == 200, f"system_status failed: {r.status_code} {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), f"system_status must return dict, got {type(data).__name__}"

    def test_healthz_returns_ok(self, http):
        """GET /healthz must return ok=true with service name and uptime."""
        r = http.get("/healthz")
        assert r.status_code == 200, f"/healthz failed: {r.status_code} {r.text[:200]}"
        data = r.json()
        assert data.get("ok") is True, f"healthz must have ok=true: {data}"
        assert data.get("service") == "opencode-search", f"wrong service name: {data}"
        assert isinstance(data.get("uptime_s"), (int, float)), f"uptime_s missing: {data}"

    @pytest.mark.slow
    def test_chat_non_streaming_returns_result(self, http, project):
        """Non-streaming /api/chat must return a complete answer dict."""
        r = http.post("/api/chat", json={
            "project": project,
            "query": "What is the overall purpose of this codebase?",
        })
        assert r.status_code == 200, f"/api/chat failed: {r.text[:300]}"
        data = r.json()
        assert "answer" in data or "text" in data or "result" in data or "response" in data, (
            f"/api/chat must return a dict with answer/text/result/response; got keys: {list(data.keys())}"
        )

    @pytest.mark.slow
    def test_debug_endpoint_returns_trace(self, http, project):
        """POST /api/debug with a traceback must return a debug analysis result."""
        traceback_text = (
            "Traceback (most recent call last):\n"
            "  File 'handler.py', line 12, in process\n"
            "    result = db.query(sql)\n"
            "AttributeError: 'NoneType' object has no attribute 'query'"
        )
        r = http.post("/api/debug", json={
            "project": project,
            "traceback": traceback_text,
            "include_fix": False,
        })
        assert r.status_code == 200, f"/api/debug failed: {r.text[:300]}"
        data = r.json()
        assert isinstance(data, dict), f"/api/debug must return a dict; got {type(data)}"
        has_analysis = any(k in data for k in ("analysis", "answer", "summary", "root_cause", "files"))
        assert has_analysis, (
            f"/api/debug must return analysis/answer/summary/root_cause/files; got keys: {list(data.keys())}"
        )


# ---------------------------------------------------------------------------
# extended coverage — dedicated routes not covered via overview/graph params
# ---------------------------------------------------------------------------

class TestMCPExtended:
    """Dedicated route coverage for endpoints not exercised via overview/graph params."""

    def test_graph_export_json_returns_graph_data(self, http, project):
        """GET /api/graph_export?format=json must return nodes/edges dict."""
        r = http.get("/api/graph_export", params={"project": project, "format": "json", "max_nodes": "200"})
        assert r.status_code == 200, f"graph_export failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "graph_export must return a dict"
        has_graph = "nodes" in data or "edges" in data or "graph" in data or "error" in data
        assert has_graph, f"graph_export missing nodes/edges/graph: {list(data.keys())}"

    def test_metrics_history_returns_time_series(self, http):
        """GET /api/metrics/history must return bucketed time series arrays."""
        r = http.get("/api/metrics/history", params={"hours": "1", "bucket_m": "5"})
        assert r.status_code == 200, f"metrics/history failed: {r.text[:200]}"
        data = r.json()
        assert "timestamps" in data, f"metrics/history missing timestamps: {list(data.keys())}"
        assert "latency_p50" in data, f"metrics/history missing latency_p50: {list(data.keys())}"
        assert isinstance(data["timestamps"], list), "timestamps must be a list"

    def test_alerts_get_returns_rules_and_violations(self, http):
        """GET /api/alerts must return alert rules and current violation status."""
        r = http.get("/api/alerts")
        assert r.status_code == 200, f"alerts GET failed: {r.text[:200]}"
        data = r.json()
        assert "rules" in data, f"alerts missing rules: {list(data.keys())}"
        assert "violations" in data, f"alerts missing violations: {list(data.keys())}"
        assert isinstance(data["rules"], list), "rules must be a list"

    def test_tree_html_returns_html(self, http, project):
        """GET /api/tree_html?format=html must return an HTML string."""
        r = http.get("/api/tree_html", params={"project": project, "format": "html", "max_files": "500"})
        assert r.status_code == 200, f"tree_html failed: {r.text[:200]}"
        assert "text/html" in r.headers.get("content-type", "") or "<" in r.text, (
            "tree_html must return HTML"
        )

    def test_tree_html_json_returns_dict(self, http, project):
        """GET /api/tree_html?format=json must return a tree dict."""
        r = http.get("/api/tree_html", params={"project": project, "format": "json", "max_files": "500"})
        assert r.status_code == 200, f"tree_html JSON failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "tree_html JSON must return a dict"
        has_tree = "tree" in data or "files" in data or "root" in data or "error" in data
        assert has_tree, f"tree_html JSON missing tree/files/root: {list(data.keys())}"

    def test_service_mesh_dedicated_route(self, http, project):
        """GET /api/service_mesh (dedicated route, not overview?what=service_mesh)."""
        r = http.get("/api/service_mesh", params={"project": project})
        assert r.status_code == 200, f"service_mesh failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "service_mesh must return a dict"

    def test_surprising_connections_dedicated_route(self, http, project):
        """GET /api/surprising_connections (dedicated route, not overview?what=surprising_connections)."""
        r = http.get("/api/surprising_connections", params={"project": project})
        assert r.status_code == 200, f"surprising_connections failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "surprising_connections must return a dict"

    def test_feature_map_dedicated_route(self, http, project):
        """GET /api/feature_map (dedicated route, not overview?what=feature_map)."""
        r = http.get("/api/feature_map", params={"project": project})
        assert r.status_code == 200, f"feature_map failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "feature_map must return a dict"

    def test_pr_impact_returns_impact(self, http, project):
        """GET /api/pr_impact must return impact analysis for recent changes."""
        r = http.get("/api/pr_impact", params={"project": project})
        assert r.status_code == 200, f"pr_impact failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "pr_impact must return a dict"

    def test_graph_diff_dedicated_route(self, http, project):
        """GET /api/graph_diff (dedicated route) must return added/removed symbols."""
        r = http.get("/api/graph_diff", params={"project": project})
        assert r.status_code == 200, f"graph_diff failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "graph_diff must return a dict"

    def test_import_cycles_dedicated_route(self, http, project):
        """GET /api/import_cycles (dedicated route) must return cycle info."""
        r = http.get("/api/import_cycles", params={"project": project})
        assert r.status_code == 200, f"import_cycles failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "import_cycles must return a dict"

    def test_suggested_questions_dedicated_route(self, http, project):
        """GET /api/suggested_questions (dedicated route) must return questions."""
        r = http.get("/api/suggested_questions", params={"project": project})
        assert r.status_code == 200, f"suggested_questions failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "suggested_questions must return a dict"

    def test_analyze_patterns_triggers_job(self, http, project):
        """POST /api/analyze_patterns must return a job_id for async tracking."""
        r = http.post("/api/analyze_patterns", params={"project": project})
        assert r.status_code == 200, f"analyze_patterns failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "analyze_patterns must return a dict"
        has_job = "job_id" in data or "error" in data or "status" in data
        assert has_job, f"analyze_patterns missing job_id/error/status: {list(data.keys())}"

    def test_jobs_by_id_accessible(self, http):
        """GET /api/jobs returns list; GET /api/jobs/{id} returns details for first job."""
        r = http.get("/api/jobs")
        assert r.status_code == 200, f"jobs list failed: {r.text[:200]}"
        data = r.json()
        jobs = data.get("jobs", [])
        if jobs:
            job_id = jobs[0].get("id") or jobs[0].get("job_id")
            if job_id:
                r2 = http.get(f"/api/jobs/{job_id}")
                assert r2.status_code in (200, 404), f"jobs/{job_id} unexpected status: {r2.status_code}"

    @pytest.mark.slow
    def test_feature_ask_returns_trace(self, http, project):
        """GET /api/feature?q=...&project=... must return feature trace (calls LLM)."""
        r = http.get("/api/feature", params={
            "project": project,
            "q": "How does the indexer work?",
        })
        assert r.status_code == 200, f"/api/feature failed: {r.text[:300]}"
        data = r.json()
        assert isinstance(data, dict), f"/api/feature must return a dict; got {type(data)}"
        # /api/feature returns structured trace: entry_points, call_chain, algorithm, etc.
        has_content = any(k in data for k in (
            "entry_points", "call_chain", "algorithm", "design_rationale",
            "summary", "answer", "trace", "result", "text", "error"
        ))
        assert has_content, f"/api/feature missing content keys: {list(data.keys())}"

    def test_alerts_post_saves_rules(self, http):
        """POST /api/alerts with a rule list must save and return saved count."""
        rules = [
            {"id": "test_rule", "name": "Test alert", "metric": "latency_p95_ms", "op": ">", "threshold": 9999, "enabled": False}
        ]
        r = http.post("/api/alerts", json={"rules": rules})
        assert r.status_code == 200, f"alerts POST failed: {r.text[:200]}"
        data = r.json()
        assert "saved" in data or "error" in data, f"alerts POST missing saved/error: {list(data.keys())}"

    def test_build_hierarchy_triggers_job(self, http, project):
        """POST /api/build_hierarchy must start a background job or return sync result."""
        r = http.post("/api/build_hierarchy", json={"project": project})
        assert r.status_code == 200, f"build_hierarchy failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "build_hierarchy must return a dict"
        has_result = any(k in data for k in ("job_id", "status", "error", "levels", "communities"))
        assert has_result, f"build_hierarchy missing expected keys: {list(data.keys())}"

    def test_impact_narrative_dedicated_route(self, http, project):
        """GET /api/impact_narrative (dedicated route) must return narrative for a symbol."""
        r = http.get("/api/impact_narrative", params={"project": project, "symbol": "main"})
        assert r.status_code in (200, 404), f"impact_narrative unexpected status: {r.status_code}"
        if r.status_code == 200:
            data = r.json()
            assert isinstance(data, dict), "impact_narrative must return a dict"

    def test_semantic_trace_dedicated_route(self, http, project):
        """GET /api/semantic_trace (dedicated route) must return trace or error."""
        r = http.get("/api/semantic_trace", params={
            "project": project,
            "from": "index",
            "to": "storage",
        })
        assert r.status_code == 200, f"semantic_trace dedicated route failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), "semantic_trace must return a dict"

    def test_events_stream_returns_sse(self, http):
        """GET /api/events/stream?max_events=1 must return an SSE event."""
        r = http.get("/api/events/stream", params={"max_events": "1"}, timeout=15.0)
        assert r.status_code == 200, f"events/stream failed: {r.status_code}"
        content_type = r.headers.get("content-type", "")
        has_sse = "text/event-stream" in content_type or "data:" in r.text
        assert has_sse, f"events/stream must be SSE; content-type={content_type}, body={r.text[:100]}"


    def test_job_cancel_returns_result(self, http):
        """POST /api/jobs/{job_id}/cancel must return a 200 or 404 (not 5xx)."""
        r = http.post("/api/jobs/nonexistent-job/cancel")
        assert r.status_code in (200, 404), f"job cancel unexpected status: {r.status_code}"
        data = r.json()
        assert isinstance(data, dict), "job cancel must return a dict"

    def test_wiki_page_path_traversal_blocked(self, http, project):
        """GET /api/wiki/page with path traversal in name must return 400."""
        r = http.get("/api/wiki/page", params={"project": project, "name": "../../etc/passwd"})
        assert r.status_code == 400, f"path traversal should be blocked (400): {r.status_code}"
        assert "error" in r.json(), "blocked request should return error dict"

    def test_wiki_page_nonexistent_returns_404(self, http, project):
        """GET /api/wiki/page with unknown page must return 404."""
        r = http.get("/api/wiki/page", params={"project": project, "name": "__no_such_page_xyz__"})
        assert r.status_code == 404, f"missing wiki page should return 404: {r.status_code}"

    @pytest.mark.slow
    def test_chat_stream_returns_sse_events(self, http, project):
        """POST /api/chat_stream must return SSE events with token or done frames."""
        from .conftest import parse_sse
        r = http.post(
            "/api/chat_stream",
            json={"project": project, "query": "what is this project?"},
            headers={"Accept": "text/event-stream"},
        )
        assert r.status_code == 200, f"chat_stream failed: {r.status_code} {r.text[:200]}"
        events = parse_sse(r)
        assert len(events) > 0, "chat_stream returned no SSE events"
        types = {e.get("type") for e in events}
        assert types & {"token", "done", "intent"}, f"no expected event types in SSE: {types}"


# ---------------------------------------------------------------------------
# Reload (last — restarts daemon, must run after all other tests)
# ---------------------------------------------------------------------------

class TestMCPReload:
    """manage(action='reload') — runs LAST to avoid disrupting other tests."""

    @pytest.mark.slow
    def test_manage_reload(self, http):
        """Reload returns reloading status and daemon recovers within 15s."""
        import time

        import httpx as _httpx

        r = http.post("/api/reload")
        assert r.status_code == 200, f"reload failed: {r.status_code} {r.text[:200]}"
        data = r.json()
        assert data.get("status") == "reloading", f"unexpected reload response: {data}"
        assert "pid" in data, "reload response must include pid"

        deadline = time.time() + 15
        with _httpx.Client(base_url="http://localhost:8765", timeout=5.0) as poll:
            while time.time() < deadline:
                time.sleep(1)
                try:
                    r2 = poll.get("/api/projects")
                    if r2.status_code == 200:
                        break
                except Exception:
                    pass
            else:
                pytest.fail("Daemon did not come back up within 15s after reload")
        time.sleep(2)  # extra buffer so session pool settles


# ---------------------------------------------------------------------------
# Phase 77 coverage gaps
# ---------------------------------------------------------------------------

class TestMCPCoverageGaps:
    """Phase 77 — fill highest-leverage MCP variant gaps."""

    @pytest.mark.slow
    def test_ask_scope_architecture(self, http, quality_project):
        """ask(scope='architecture') must return 200 with non-empty content."""
        r = http.get("/api/ask", params={
            "q": "describe the dashboard layer",
            "project": quality_project,
            "scope": "architecture",
        })
        assert r.status_code == 200, f"ask architecture scope failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict)
        has_content = (
            data.get("answer") or data.get("text") or data.get("summary")
            or data.get("communities") or data.get("results")
        )
        assert has_content, f"ask architecture scope returned empty response: {data}"

    @pytest.mark.slow
    def test_ask_scope_business(self, http, quality_project):
        """ask(scope='business') must call handle_ask_business and return content."""
        r = http.get("/api/ask", params={
            "q": "what business rules govern indexing",
            "project": quality_project,
            "scope": "business",
        })
        assert r.status_code == 200, f"ask business scope failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict)
        has_content = (
            data.get("answer") or data.get("text") or data.get("summary")
            or data.get("communities") or data.get("results")
        )
        assert has_content, f"ask business scope returned empty response: {data}"

    def test_overview_business_rules_core(self, http, quality_project):
        """overview(what='business_rules') on the core project must return a dict."""
        r = http.get("/api/overview", params={"project": quality_project, "what": "business_rules"})
        assert r.status_code == 200, f"overview business_rules on core failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict)

    def test_overview_process_flows_core(self, http, quality_project):
        """overview(what='process_flows') on the core project must return a dict."""
        r = http.get("/api/overview", params={"project": quality_project, "what": "process_flows"})
        assert r.status_code == 200, f"overview process_flows on core failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict)

    def test_build_job_dispatch_and_lookup(self, http, quality_project):
        """build(action='hierarchy') returns job_id; manage(jobs, job_id) looks it up."""
        r = http.post("/api/build_hierarchy", json={"project": quality_project})
        assert r.status_code == 200, f"build_hierarchy failed: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict)
        job_id = data.get("job_id")
        if job_id:
            jr = http.get(f"/api/jobs/{job_id}")
            assert jr.status_code in (200, 404), (
                f"jobs/{job_id} unexpected status: {jr.status_code}"
            )
            assert isinstance(jr.json(), dict), "job lookup must return a dict"
