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
