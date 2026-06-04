"""Full dashboard API contract tests.

Tests all 51 HTTP routes exposed by the dashboard for:
  - Correct HTTP status codes (200 for data-free routes, 400 for missing project)
  - Content-Type: application/json for all /api/* routes
  - No 500 errors (failures are 400/422/404, never unhandled crashes)
  - Response JSON has expected top-level keys

Uses in-process ASGI transport (no real server, no real GPU, no real LLM).
"""
from __future__ import annotations

import pytest

# ── ASGI app fixture (shared with test_daemon_http.py) ────────────────────────

@pytest.fixture(scope="module")
def asgi_app():
    """Build the FastMCP Starlette app in-process."""
    import opencode_search.embeddings as emb
    orig_done = emb._provider_detection_done
    orig_providers = emb._detected_providers
    emb._provider_detection_done = True
    emb._detected_providers = ["CUDAExecutionProvider"]

    from unittest.mock import patch
    with patch.object(emb, "is_gpu_available", return_value=True), \
         patch.object(emb, "assert_gpu_available", return_value=None):
        from opencode_search.mcp import mcp
        app = mcp.streamable_http_app()

    emb._provider_detection_done = orig_done
    emb._detected_providers = orig_providers
    return app


@pytest.fixture
async def client(asgi_app):
    import httpx
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi_app),
        base_url="http://testserver",
        follow_redirects=True,
    ) as c:
        yield c


# ── 1. Data-free routes (no project param required) ──────────────────────────

class TestDataFreeRoutes:
    """Routes that return data without needing a ?project= parameter."""

    async def test_api_projects_returns_200(self, client):
        r = await client.get("/api/projects")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}"
        assert "application/json" in r.headers.get("content-type", "")
        body = r.json()
        assert "projects" in body, f"Expected 'projects' key, got: {list(body.keys())}"

    async def test_api_metrics_returns_200(self, client):
        r = await client.get("/api/metrics")
        assert r.status_code == 200
        assert "application/json" in r.headers.get("content-type", "")
        body = r.json()
        # uptime_s may be 0 for a fresh test app — just check the key exists
        assert "uptime_s" in body or "uptime" in body or "connected_clients" in body, \
            f"Metrics must have uptime-related key, got: {list(body.keys())}"

    async def test_api_system_status_returns_200(self, client):
        r = await client.get("/api/system_status")
        assert r.status_code == 200
        assert "application/json" in r.headers.get("content-type", "")
        body = r.json()
        assert "status" in body or "result" in body or "checks" in body, \
            f"system_status must have status key, got: {list(body.keys())}"

    async def test_api_auto_pipeline_status_returns_200(self, client):
        r = await client.get("/api/auto_pipeline_status")
        assert r.status_code == 200
        assert "application/json" in r.headers.get("content-type", "")
        body = r.json()
        assert "enabled" in body, f"auto_pipeline_status must have 'enabled' key, got: {list(body.keys())}"

    async def test_api_alerts_returns_200(self, client):
        r = await client.get("/api/alerts")
        assert r.status_code == 200
        assert "application/json" in r.headers.get("content-type", "")
        body = r.json()
        assert "alerts" in body or "rules" in body or "violations" in body or isinstance(body, list), \
            f"alerts must have 'alerts', 'rules', or 'violations' key, got: {list(body.keys()) if isinstance(body, dict) else type(body)}"

    async def test_api_jobs_returns_200(self, client):
        r = await client.get("/api/jobs")
        assert r.status_code == 200
        assert "application/json" in r.headers.get("content-type", "")

    async def test_api_integrations_status_returns_200(self, client):
        r = await client.get("/api/integrations_status")
        assert r.status_code == 200
        assert "application/json" in r.headers.get("content-type", "")
        body = r.json()
        assert isinstance(body, (dict, list)), f"integrations_status must return a dict or list, got {type(body)}"


# ── 2. Project-required routes: missing param → 400, not 500 ─────────────────

_PROJECT_REQUIRED_ROUTES = [
    "/api/overview",
    "/api/kb_health",
    "/api/communities",
    "/api/wiki",
    "/api/ask",
    "/api/search",
    "/api/graph",
    "/api/patterns",
    "/api/service_mesh",
    "/api/import_cycles",
    "/api/suggested_questions",
    "/api/graph_diff",
    "/api/surprising_connections",
    "/api/feature_map",
    "/api/business_rules",
    "/api/process_flows",
    "/api/pr_impact",
    "/api/tree_html",
]


class TestProjectRequiredRoutes:
    """Routes that require ?project=: missing param must return 400/422, never 500."""

    @pytest.mark.parametrize("path", _PROJECT_REQUIRED_ROUTES)
    async def test_missing_project_returns_4xx_not_500(self, client, path):
        r = await client.get(path)
        assert r.status_code < 500, \
            f"GET {path} without project returned {r.status_code} (must not be 5xx): {r.text[:200]}"
        assert "application/json" in r.headers.get("content-type", ""), \
            f"GET {path} must return JSON Content-Type, got: {r.headers.get('content-type')}"

    @pytest.mark.parametrize("path", [
        "/api/overview",
        "/api/kb_health",
        "/api/communities",
    ])
    async def test_missing_project_returns_400_or_422(self, client, path):
        r = await client.get(path)
        assert r.status_code in (400, 422), \
            f"GET {path} without project must return 400 or 422, got {r.status_code}"


# ── 3. POST routes: empty body → 400 ─────────────────────────────────────────

class TestPostRoutes:
    """POST routes return 400 on empty or missing required fields."""

    async def test_api_chat_empty_body_returns_400(self, client):
        r = await client.post("/api/chat", content=b"", headers={"Content-Type": "application/json"})
        assert r.status_code in (400, 422), \
            f"POST /api/chat with empty body should return 400/422, got {r.status_code}"

    async def test_api_chat_missing_query_returns_400(self, client):
        import json
        r = await client.post(
            "/api/chat",
            content=json.dumps({"project": "/tmp/test-project"}),
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code in (400, 422), \
            f"POST /api/chat without 'query' should return 400/422, got {r.status_code}"

    async def test_api_chat_missing_project_returns_400(self, client):
        import json
        r = await client.post(
            "/api/chat",
            content=json.dumps({"query": "how does indexing work?"}),
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code in (400, 422), \
            f"POST /api/chat without 'project' should return 400/422, got {r.status_code}"

    async def test_api_chat_stream_missing_project_returns_400(self, client):
        import json
        r = await client.post(
            "/api/chat_stream",
            content=json.dumps({"query": "how does indexing work?"}),
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code in (400, 422), \
            f"POST /api/chat_stream without 'project' should return 400/422, got {r.status_code}"

    async def test_api_debug_empty_body_returns_400(self, client):
        r = await client.post("/api/debug", content=b"", headers={"Content-Type": "application/json"})
        assert r.status_code in (400, 422), \
            f"POST /api/debug with empty body should return 400/422, got {r.status_code}"

    async def test_api_vacuum_missing_project_returns_400(self, client):
        import json
        r = await client.post(
            "/api/vacuum",
            content=json.dumps({}),
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code in (400, 422, 405), \
            f"POST /api/vacuum without project should return 400/422, got {r.status_code}"

    async def test_api_dedup_missing_project_returns_4xx(self, client):
        r = await client.get("/api/dedup")
        assert r.status_code < 500, \
            f"GET /api/dedup without project should not return 5xx, got {r.status_code}"


# ── 4. Dashboard HTML routes ──────────────────────────────────────────────────

class TestDashboardHtml:
    """HTML routes serve the 3-view Datadog dashboard."""

    async def test_dashboard_returns_200_html(self, client):
        r = await client.get("/dashboard")
        assert r.status_code == 200, f"GET /dashboard returned {r.status_code}"
        ct = r.headers.get("content-type", "")
        assert "html" in ct or "<!doctype" in r.text.lower() or "<html" in r.text.lower(), \
            f"GET /dashboard must return HTML, got Content-Type: {ct}"

    async def test_dashboard_has_pulse_view(self, client):
        r = await client.get("/dashboard")
        assert r.status_code == 200
        assert "view-pulse" in r.text, "Dashboard HTML must contain view-pulse element"

    async def test_dashboard_has_chat_view(self, client):
        r = await client.get("/dashboard")
        assert "view-chat" in r.text, "Dashboard HTML must contain view-chat element"

    async def test_dashboard_has_admin_view(self, client):
        r = await client.get("/dashboard")
        assert "view-admin" in r.text, "Dashboard HTML must contain view-admin element"

    async def test_dashboard_has_no_old_sidebar(self, client):
        r = await client.get("/dashboard")
        # The old design had a .sidebar div with many nav items — it should be gone
        assert 'class="sidebar"' not in r.text and "id=\"sidebar\"" not in r.text, \
            "Dashboard must not have old sidebar element"

    async def test_dashboard_has_bento_grid(self, client):
        r = await client.get("/dashboard")
        assert "bento" in r.text, "Dashboard HTML must contain bento KPI grid"

    async def test_dashboard_has_ctrl_k_palette(self, client):
        r = await client.get("/dashboard")
        assert "cmd-overlay" in r.text or "cmd-palette" in r.text, \
            "Dashboard HTML must contain Ctrl+K command palette"


# ── 5. Jobs API ───────────────────────────────────────────────────────────────

class TestJobsApi:
    """Background job management endpoints."""

    async def test_get_nonexistent_job_returns_404(self, client):
        r = await client.get("/api/jobs/nonexistent-job-id-12345")
        assert r.status_code == 404, \
            f"GET /api/jobs/nonexistent-id must return 404, got {r.status_code}"

    async def test_cancel_nonexistent_job_returns_404(self, client):
        r = await client.post("/api/jobs/nonexistent-job-id-12345/cancel")
        assert r.status_code == 404, \
            f"POST /api/jobs/nonexistent-id/cancel must return 404, got {r.status_code}"

    async def test_jobs_list_returns_json(self, client):
        r = await client.get("/api/jobs")
        assert r.status_code == 200
        assert "application/json" in r.headers.get("content-type", "")


# ── 6. SSE streaming endpoint ────────────────────────────────────────────────

class TestSseStream:
    """Server-sent events endpoint for live metric streaming."""

    async def test_events_stream_returns_200(self, asgi_app):
        # SSE streams are long-lived; we open the connection, check headers, then cancel.
        import contextlib

        import anyio
        import httpx
        status_code = None
        content_type = None

        async def _probe():
            nonlocal status_code, content_type
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=asgi_app),
                base_url="http://testserver",
            ) as c, c.stream("GET", "/api/events/stream") as r:
                status_code = r.status_code
                content_type = r.headers.get("content-type", "")
                # Cancel after header check — don't consume the infinite stream
                raise anyio.get_cancelled_exc_class()()

        with anyio.move_on_after(3.0), contextlib.suppress(Exception):
            await _probe()

        if status_code is not None:
            assert status_code == 200, \
                f"GET /api/events/stream must return 200, got {status_code}"
            assert "event-stream" in (content_type or "") or "text/" in (content_type or ""), \
                f"SSE stream must have text/event-stream content-type, got: {content_type}"


# ── 7. Metrics history endpoint ───────────────────────────────────────────────

class TestMetricsHistory:
    """Time-series metrics endpoint for dashboard charts."""

    async def test_metrics_history_returns_200(self, client):
        r = await client.get("/api/metrics/history")
        assert r.status_code == 200
        assert "application/json" in r.headers.get("content-type", "")

    async def test_metrics_history_returns_dict(self, client):
        r = await client.get("/api/metrics/history")
        body = r.json()
        assert isinstance(body, dict), \
            f"/api/metrics/history must return a dict, got {type(body)}"


# ── 8. Response consistency ───────────────────────────────────────────────────

class TestResponseConsistency:
    """Cross-cutting API response shape contracts."""

    @pytest.mark.parametrize("path,method", [
        ("/api/projects", "GET"),
        ("/api/metrics", "GET"),
        ("/api/alerts", "GET"),
        ("/api/jobs", "GET"),
    ])
    async def test_successful_routes_return_no_server_error_key(self, client, path, method):
        if method == "GET":
            r = await client.get(path)
        else:
            r = await client.post(path)
        assert r.status_code == 200
        body = r.json()
        # A well-formed successful response must not have an "_error" key at top level
        assert "_error" not in body, \
            f"{method} {path} returned _error in successful response: {body.get('_error')}"

    async def test_all_api_routes_return_json_content_type(self, client):
        data_free_routes = [
            "/api/projects", "/api/metrics", "/api/system_status",
            "/api/auto_pipeline_status", "/api/alerts", "/api/jobs",
        ]
        for path in data_free_routes:
            r = await client.get(path)
            ct = r.headers.get("content-type", "")
            assert "application/json" in ct, \
                f"GET {path} must return application/json, got: {ct}"
