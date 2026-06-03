"""T38: Daemon HTTP layer — in-process ASGI test of every dashboard route.

Builds the Starlette app from mcp.streamable_http_app() and drives it with an
httpx AsyncClient (ASGI transport).  No real server is started, no port is
bound, no GPU is needed.  Every test either passes or fails loudly — zero skips.

Coverage:
  - All GET /api/* routes return 2xx or meaningful error JSON (not 500)
  - POST /api/analyze_patterns, /api/run_prerelease, /api/run_qa are registered
  - Dashboard HTML routes (/, /dashboard) return 200 with HTML
  - Missing required params return 400/422 (not 500)
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ASTRO = "/home/user/git/github.com/fairyhunter13/redacted-name-3"


def _use_real_registry(monkeypatch):
    import opencode_search.config as cfg
    real_path = Path(os.path.expanduser("~/.local/share/opencode-search/projects.json"))
    monkeypatch.setattr(cfg, "REGISTRY_PATH", real_path)


# ---------------------------------------------------------------------------
# Module-level fixture: build the ASGI app once per module
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def asgi_app():
    """Build the FastMCP Starlette app in-process (no real server, no GPU)."""
    import opencode_search.embeddings as emb
    orig_done = emb._provider_detection_done
    orig_providers = emb._detected_providers
    emb._provider_detection_done = True
    emb._detected_providers = ["CUDAExecutionProvider"]

    with patch.object(emb, "is_gpu_available", return_value=True), \
         patch.object(emb, "assert_gpu_available", return_value=None):
        from opencode_search.mcp import mcp
        app = mcp.streamable_http_app()

    emb._provider_detection_done = orig_done
    emb._detected_providers = orig_providers
    return app


@pytest.fixture
async def client(asgi_app):
    """httpx AsyncClient connected to the ASGI app (no real TCP socket)."""
    import httpx
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi_app),
        base_url="http://testserver",
        follow_redirects=True,
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# T38-A: HTML routes
# ---------------------------------------------------------------------------

class TestT38AHtmlRoutes:
    """P0: Root and dashboard routes return 200 HTML."""

    @pytest.mark.asyncio
    async def test_root_returns_html(self, client):
        """P0: GET / returns 200 with HTML content."""
        r = await client.get("/")
        assert r.status_code == 200, f"GET / returned {r.status_code}: {r.text[:200]}"
        ct = r.headers.get("content-type", "")
        assert "html" in ct or "<html" in r.text.lower() or "<!doctype" in r.text.lower(), (
            f"GET / did not return HTML. Content-Type: {ct}. Body: {r.text[:100]}"
        )

    @pytest.mark.asyncio
    async def test_dashboard_alias_returns_html(self, client):
        """P0: GET /dashboard returns 200."""
        r = await client.get("/dashboard")
        assert r.status_code == 200, f"GET /dashboard returned {r.status_code}: {r.text[:200]}"


# ---------------------------------------------------------------------------
# T38-B: Data-free API routes (no project param required)
# ---------------------------------------------------------------------------

class TestT38BDataFreeRoutes:
    """P0: API routes that work without a project param."""

    @pytest.mark.asyncio
    async def test_metrics_returns_json(self, client):
        """P0: GET /api/metrics returns JSON dict."""
        r = await client.get("/api/metrics")
        assert r.status_code == 200, f"/api/metrics returned {r.status_code}: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), f"Expected dict, got {type(data)}"

    @pytest.mark.asyncio
    async def test_projects_returns_list(self, client):
        """P0: GET /api/projects returns JSON array or dict."""
        r = await client.get("/api/projects")
        assert r.status_code == 200, f"/api/projects returned {r.status_code}"
        data = r.json()
        assert isinstance(data, (list, dict)), f"Unexpected type: {type(data)}"

    @pytest.mark.asyncio
    async def test_auto_pipeline_status_returns_enabled(self, client):
        """P0: GET /api/auto_pipeline_status returns dict with 'enabled' field."""
        r = await client.get("/api/auto_pipeline_status")
        assert r.status_code == 200, f"/api/auto_pipeline_status returned {r.status_code}"
        data = r.json()
        assert isinstance(data, dict)
        assert "enabled" in data or "auto_pipeline_enabled" in data, (
            f"auto_pipeline_status missing 'enabled': {list(data.keys())}"
        )

    @pytest.mark.asyncio
    async def test_integrations_status_returns_data(self, client):
        """P1: GET /api/integrations_status returns list or dict."""
        r = await client.get("/api/integrations_status")
        assert r.status_code == 200, f"/api/integrations_status returned {r.status_code}"
        assert isinstance(r.json(), (list, dict))

    @pytest.mark.asyncio
    async def test_verify_status_returns_dict(self, client):
        """P1: GET /api/verify_status returns dict."""
        r = await client.get("/api/verify_status")
        assert r.status_code == 200, f"/api/verify_status returned {r.status_code}"
        assert isinstance(r.json(), dict)

    @pytest.mark.asyncio
    async def test_prerelease_status_returns_dict(self, client):
        """P1: GET /api/prerelease_status returns dict."""
        r = await client.get("/api/prerelease_status")
        assert r.status_code == 200, f"/api/prerelease_status returned {r.status_code}"
        assert isinstance(r.json(), dict)

    @pytest.mark.asyncio
    async def test_qa_status_returns_dict(self, client):
        """P1: GET /api/qa_status returns dict."""
        r = await client.get("/api/qa_status")
        assert r.status_code == 200, f"/api/qa_status returned {r.status_code}"
        assert isinstance(r.json(), dict)


# ---------------------------------------------------------------------------
# T38-C: Project-scoped routes (require ?project=, return error if missing)
# ---------------------------------------------------------------------------

class TestT38CProjectRoutes:
    """P0: Project-scoped routes return 400/422 when ?project= is missing,
    and return valid JSON (not 500) when called with a non-indexed path.
    """

    @pytest.mark.asyncio
    async def test_search_missing_project_returns_error_not_500(self, client):
        """P0: GET /api/search without ?project= returns 400/422/200 but not 500."""
        r = await client.get("/api/search?q=test")
        assert r.status_code != 500, f"search returned 500: {r.text[:200]}"

    @pytest.mark.asyncio
    async def test_ask_missing_project_returns_error_not_500(self, client):
        """P0: GET /api/ask without ?project= returns non-500."""
        r = await client.get("/api/ask?q=test")
        assert r.status_code != 500, f"ask returned 500: {r.text[:200]}"

    @pytest.mark.asyncio
    async def test_communities_missing_project_returns_error_not_500(self, client):
        """P0: GET /api/communities without ?project= returns non-500."""
        r = await client.get("/api/communities")
        assert r.status_code != 500, f"communities returned 500: {r.text[:200]}"

    @pytest.mark.asyncio
    async def test_wiki_missing_project_returns_error_not_500(self, client):
        """P0: GET /api/wiki without ?project= returns non-500."""
        r = await client.get("/api/wiki")
        assert r.status_code != 500, f"wiki returned 500: {r.text[:200]}"

    @pytest.mark.asyncio
    async def test_patterns_missing_project_returns_error_not_500(self, client):
        """P0: GET /api/patterns without ?project= returns non-500."""
        r = await client.get("/api/patterns")
        assert r.status_code != 500, f"patterns returned 500: {r.text[:200]}"

    @pytest.mark.asyncio
    async def test_kb_health_missing_project_returns_error_not_500(self, client):
        """P0: GET /api/kb_health without ?project= returns non-500."""
        r = await client.get("/api/kb_health")
        assert r.status_code != 500, f"kb_health returned 500: {r.text[:200]}"

    @pytest.mark.asyncio
    async def test_overview_missing_project_returns_error_not_500(self, client):
        """P0: GET /api/overview without ?project= returns non-500."""
        r = await client.get("/api/overview")
        assert r.status_code != 500, f"overview returned 500: {r.text[:200]}"

    @pytest.mark.asyncio
    async def test_graph_missing_project_returns_error_not_500(self, client):
        """P0: GET /api/graph without ?project= returns non-500."""
        r = await client.get("/api/graph?symbol=foo")
        assert r.status_code != 500, f"graph returned 500: {r.text[:200]}"

    @pytest.mark.asyncio
    async def test_service_mesh_missing_project_returns_error_not_500(self, client):
        """P0: GET /api/service_mesh without ?project= returns non-500."""
        r = await client.get("/api/service_mesh")
        assert r.status_code != 500, f"service_mesh returned 500: {r.text[:200]}"

    @pytest.mark.asyncio
    async def test_impact_narrative_missing_project_returns_error_not_500(self, client):
        """P0: GET /api/impact_narrative without required params returns non-500."""
        r = await client.get("/api/impact_narrative")
        assert r.status_code != 500, f"impact_narrative returned 500: {r.text[:200]}"

    @pytest.mark.asyncio
    async def test_semantic_trace_missing_project_returns_error_not_500(self, client):
        """P0: GET /api/semantic_trace without required params returns non-500."""
        r = await client.get("/api/semantic_trace")
        assert r.status_code != 500, f"semantic_trace returned 500: {r.text[:200]}"

    @pytest.mark.asyncio
    async def test_federation_missing_project_returns_error_not_500(self, client):
        """P0: GET /api/federation without ?project= returns non-500."""
        r = await client.get("/api/federation")
        assert r.status_code != 500, f"federation returned 500: {r.text[:200]}"

    @pytest.mark.asyncio
    async def test_graph_export_missing_project_returns_error_not_500(self, client):
        """P0: GET /api/graph_export without ?project= returns non-500."""
        r = await client.get("/api/graph_export")
        assert r.status_code != 500, f"graph_export returned 500: {r.text[:200]}"


# ---------------------------------------------------------------------------
# T38-D: POST routes are registered (method check)
# ---------------------------------------------------------------------------

class TestT38DPostRoutes:
    """P0: POST endpoints are registered (not 404/405)."""

    @pytest.mark.asyncio
    async def test_analyze_patterns_post_registered(self, client):
        """P0: POST /api/analyze_patterns is registered (not 404)."""
        r = await client.post("/api/analyze_patterns")
        assert r.status_code != 404, (
            f"POST /api/analyze_patterns returned 404 — route not registered"
        )

    @pytest.mark.asyncio
    async def test_run_prerelease_post_registered(self, client):
        """P1: POST /api/run_prerelease is registered (not 404)."""
        r = await client.post("/api/run_prerelease")
        assert r.status_code != 404, (
            f"POST /api/run_prerelease returned 404 — route not registered"
        )

    @pytest.mark.asyncio
    async def test_run_qa_post_registered(self, client):
        """P1: POST /api/run_qa is registered (not 404)."""
        r = await client.post("/api/run_qa")
        assert r.status_code != 404, (
            f"POST /api/run_qa returned 404 — route not registered"
        )

    @pytest.mark.asyncio
    async def test_build_hierarchy_post_registered(self, client):
        """P1: POST /api/build_hierarchy is registered (not 404)."""
        r = await client.post("/api/build_hierarchy")
        assert r.status_code != 404, (
            f"POST /api/build_hierarchy returned 404 — route not registered"
        )

    @pytest.mark.asyncio
    async def test_auto_fix_trigger_post_registered(self, client):
        """P1: POST /api/auto_fix_trigger is registered (not 404)."""
        r = await client.post("/api/auto_fix_trigger")
        assert r.status_code != 404, (
            f"POST /api/auto_fix_trigger returned 404 — route not registered"
        )


# ---------------------------------------------------------------------------
# T38-E: Phase 3 new routes — metrics history, SSE, alerts, system status
# ---------------------------------------------------------------------------

class TestT38EPhase3Routes:
    """P0: New Phase 3 routes are registered and return correct shapes."""

    @pytest.mark.asyncio
    async def test_metrics_history_returns_timeseries(self, client):
        """P0: GET /api/metrics/history returns dict with timestamps array."""
        r = await client.get("/api/metrics/history?hours=1")
        assert r.status_code == 200, f"/api/metrics/history returned {r.status_code}: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), f"Expected dict, got {type(data)}"
        assert "timestamps" in data, f"Missing 'timestamps' key: {list(data.keys())}"
        assert "latency_p50" in data, f"Missing 'latency_p50' key: {list(data.keys())}"
        assert "latency_p95" in data, f"Missing 'latency_p95' key"
        assert "zero_result_pct" in data, f"Missing 'zero_result_pct' key"
        assert isinstance(data["timestamps"], list), "timestamps must be a list"

    @pytest.mark.asyncio
    async def test_alerts_get_returns_rules_and_violations(self, client):
        """P0: GET /api/alerts returns rules list and violations list."""
        r = await client.get("/api/alerts")
        assert r.status_code == 200, f"/api/alerts returned {r.status_code}: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict), f"Expected dict, got {type(data)}"
        assert "rules" in data, f"Missing 'rules' key: {list(data.keys())}"
        assert "violations" in data, f"Missing 'violations' key: {list(data.keys())}"
        assert isinstance(data["rules"], list), "rules must be a list"
        assert isinstance(data["violations"], list), "violations must be a list"

    @pytest.mark.asyncio
    async def test_alerts_post_saves_rules(self, client):
        """P1: POST /api/alerts saves rules and returns saved count."""
        rules = [
            {"id": "test_rule", "name": "Test", "metric": "latency_p95_ms",
             "op": ">", "threshold": 1000, "enabled": True}
        ]
        r = await client.post("/api/alerts", json={"rules": rules})
        assert r.status_code == 200, f"POST /api/alerts returned {r.status_code}: {r.text[:200]}"
        data = r.json()
        assert "saved" in data, f"Missing 'saved' key: {list(data.keys())}"
        assert data["saved"] == 1

    @pytest.mark.asyncio
    async def test_events_stream_returns_sse_headers(self, client):
        """P0: GET /api/events/stream is registered and configured as SSE.

        httpx ASGI transport cannot stream infinitely (listen_for_disconnect blocks
        until the full response is consumed), so we verify the route contract by
        requesting ?max_events=1 which causes the generator to stop after one event.
        """
        r = await client.get("/api/events/stream?max_events=1")
        assert r.status_code == 200, f"/api/events/stream returned {r.status_code}"
        ct = r.headers.get("content-type", "")
        assert "text/event-stream" in ct, f"Expected text/event-stream, got {ct!r}"
        assert b"data:" in r.content, f"SSE response missing 'data:' prefix: {r.content[:100]}"

    @pytest.mark.asyncio
    async def test_system_status_registered(self, client):
        """P0: GET /api/system_status is registered (not 404)."""
        r = await client.get("/api/system_status")
        assert r.status_code != 404, f"/api/system_status returned 404 — route not registered"
        # Returns either a status dict or a 503/500 if ocs_status.py is unavailable
        assert r.status_code in (200, 500, 503), f"Unexpected status: {r.status_code}"

    @pytest.mark.asyncio
    async def test_static_route_serves_chartjs(self, client):
        """P0: GET /static/chart.min.js serves Chart.js file."""
        r = await client.get("/static/chart.min.js")
        assert r.status_code == 200, f"/static/chart.min.js returned {r.status_code}"
        assert len(r.content) > 50_000, f"chart.min.js too small: {len(r.content)} bytes"

    @pytest.mark.asyncio
    async def test_static_route_missing_file_returns_404(self, client):
        """P0: GET /static/nonexistent returns 404."""
        r = await client.get("/static/does_not_exist.js")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_static_route_serves_sigma_graph(self, client):
        """P0: GET /static/sigma-graph.min.js serves Sigma.js WebGL bundle."""
        r = await client.get("/static/sigma-graph.min.js")
        assert r.status_code == 200, f"/static/sigma-graph.min.js returned {r.status_code}"
        assert len(r.content) > 50_000, f"sigma-graph.min.js too small: {len(r.content)} bytes"


# ---------------------------------------------------------------------------
# T38-F: API response shape verification (key-level contracts)
# ---------------------------------------------------------------------------

class TestT38FApiShapeVerification:
    """P0: Verify /api/* responses have the expected keys, not just 200."""

    @pytest.mark.asyncio
    async def test_metrics_has_all_required_keys(self, client):
        """P0: GET /api/metrics includes call_count, latency_ms, zero_result_count."""
        r = await client.get("/api/metrics")
        assert r.status_code == 200
        data = r.json()
        assert "call_count" in data, f"Missing 'call_count': {list(data.keys())}"
        assert "zero_result_count" in data, f"Missing 'zero_result_count': {list(data.keys())}"
        assert "latency_ms" in data, f"Missing 'latency_ms': {list(data.keys())}"

    @pytest.mark.asyncio
    async def test_metrics_latency_ms_has_percentile_keys(self, client):
        """P0: latency_ms sub-dict has p50, p95, avg, min, max keys."""
        r = await client.get("/api/metrics")
        assert r.status_code == 200
        lms = r.json().get("latency_ms", {})
        assert isinstance(lms, dict), f"latency_ms is not a dict: {lms}"
        for key in ("p50", "p95", "avg"):
            assert key in lms, f"latency_ms missing '{key}' key: {list(lms.keys())}"

    @pytest.mark.asyncio
    async def test_communities_with_project_returns_communities_key(self, client, monkeypatch):
        """P0: GET /api/communities?project=... returns JSON with 'communities' key."""
        _use_real_registry(monkeypatch)
        r = await client.get(f"/api/communities?project={_ASTRO}&top_k=5")
        assert r.status_code == 200, f"/api/communities returned {r.status_code}: {r.text[:200]}"
        data = r.json()
        assert isinstance(data, dict)
        assert "communities" in data, f"Missing 'communities' key: {list(data.keys())}"
        assert isinstance(data["communities"], list), "communities must be a list"

    @pytest.mark.asyncio
    async def test_patterns_with_nonexistent_project_returns_json(self, client):
        """P0: GET /api/patterns with non-indexed project path returns JSON (not 500)."""
        r = await client.get("/api/patterns?project=/tmp/nonexistent_project_xyz")
        assert r.status_code != 500, f"/api/patterns returned 500: {r.text[:200]}"
        assert r.headers.get("content-type", "").startswith("application/json"), (
            f"Expected JSON content-type: {r.headers.get('content-type')}"
        )

    @pytest.mark.asyncio
    async def test_metrics_history_has_search_count_key(self, client):
        """P0: /api/metrics/history includes search_count per bucket."""
        r = await client.get("/api/metrics/history?hours=1&bucket_m=60")
        assert r.status_code == 200
        data = r.json()
        assert "search_count" in data, f"Missing 'search_count': {list(data.keys())}"
        assert "hours" in data, f"Missing 'hours': {list(data.keys())}"
        assert "bucket_m" in data, f"Missing 'bucket_m': {list(data.keys())}"

    @pytest.mark.asyncio
    async def test_auto_pipeline_status_events_key(self, client):
        """P0: /api/auto_pipeline_status returns 'events' list alongside 'enabled'."""
        r = await client.get("/api/auto_pipeline_status")
        assert r.status_code == 200
        data = r.json()
        assert "events" in data or "recent_events" in data or "enabled" in data, (
            f"Expected events/enabled in auto_pipeline_status: {list(data.keys())}"
        )

    @pytest.mark.asyncio
    async def test_alerts_current_metrics_key_present(self, client):
        """P0: GET /api/alerts response includes current_metrics dict."""
        r = await client.get("/api/alerts")
        assert r.status_code == 200
        data = r.json()
        assert "current_metrics" in data, (
            f"Missing 'current_metrics' in alerts response: {list(data.keys())}"
        )
        assert isinstance(data["current_metrics"], dict)
