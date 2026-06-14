"""P5 server tests: MCP tools, HTTP routes, dashboard (no mocks)."""
import asyncio
import json

import pytest

pytestmark = pytest.mark.live


def test_mcp_has_five_tools():
    """All 5 MCP tools registered in FastMCP app."""
    from opencode_search.server.mcp import mcp
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert {"search", "ask", "graph", "overview", "index"} <= names


def test_mcp_graph_nonexistent_returns_error():
    """graph tool returns {error:...} JSON for an unindexed project."""
    from opencode_search.server.mcp import graph as graph_tool
    result = asyncio.run(graph_tool("authenticate", "/nonexistent/path", "definition"))
    data = json.loads(result)
    assert "error" in data


def test_mcp_overview_projects_returns_list():
    """P15.4: overview(what='projects') returns ≥1 real registered project."""
    from opencode_search.server.mcp import overview as overview_tool
    result = asyncio.run(overview_tool("", "projects"))
    data = json.loads(result)
    assert "projects" in data
    assert len(data["projects"]) >= 1, "daemon should have ≥1 registered project"


def test_mcp_overview_metrics():
    """P20.3: overview(what='metrics') returns chat_stream metrics dict."""
    from opencode_search.server.mcp import overview as overview_tool
    result = asyncio.run(overview_tool("", "metrics"))
    data = json.loads(result)
    assert "chat_stream" in data, f"metrics missing chat_stream key: {result}"
    assert "stream_error_count" in data["chat_stream"], f"chat_stream missing stream_error_count: {data}"


def test_mcp_index_register_remove(tmp_path):
    """index tool registers then removes a project without crashing."""
    from opencode_search.server.mcp import index as index_tool
    p = str(tmp_path)
    reg = json.loads(asyncio.run(index_tool(p, enabled=True)))
    assert reg["status"] in ("flagged", "already_registered")
    rem = json.loads(asyncio.run(index_tool(p, enabled=False)))
    assert rem["status"] in ("removed", "not_found")


def test_healthz(live_client):
    """P15.2: /healthz on the REAL daemon (production create_app)."""
    r = live_client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_dashboard_five_views(live_client):
    """P15.2: /dashboard on the REAL daemon — all 5 views present."""
    r = live_client.get("/dashboard")
    assert r.status_code == 200
    body = r.text.lower()
    for view in ("pulse", "chat", "admin", "wiki", "graph"):
        assert view in body, f"dashboard missing '{view}' view"


def test_api_projects_returns_list(live_client):
    """P15.2/P15.4: /api/projects returns ≥1 real registered project."""
    r = live_client.get("/api/projects")
    assert r.status_code == 200
    data = r.json()
    assert "projects" in data
    assert len(data["projects"]) >= 1, "live daemon should have ≥1 registered project"


def test_api_overview_projects(live_client):
    """P15.2/P15.4: /api/overview?what=projects returns ≥1 real project."""
    r = live_client.post("/api/overview", json={"what": "projects"})
    assert r.status_code == 200
    data = r.json()
    assert "projects" in data
    assert len(data["projects"]) >= 1, "live daemon should have ≥1 registered project"


def test_live_daemon_has_mcp_route(live_client):
    """P15.2 parity: production create_app() mounts /mcp (FastMCP streamable-HTTP).
    The test-only in-process app lacks this route; driving the live daemon proves
    tests exercise the real served surface, not a stripped-down variant.
    """
    # POST without MCP headers → 406 Not Acceptable (not 404 Not Found)
    r = live_client.post("/mcp", json={})
    assert r.status_code != 404, (
        f"/mcp not found — create_app() must mount FastMCP at /mcp; got {r.status_code}"
    )


def test_api_search_missing_query_returns_400():
    from starlette.testclient import TestClient

    from opencode_search.server.routes import build_test_app as create_app
    with TestClient(create_app()) as c:
        r = c.post("/api/search", json={"project": "/nonexistent"})
        assert r.status_code == 400
        assert "error" in r.json()


def test_api_search_nonexistent_project_returns_empty():
    from starlette.testclient import TestClient

    from opencode_search.server.routes import build_test_app as create_app
    with TestClient(create_app()) as c:
        r = c.post("/api/search", json={"q": "authenticate", "project": "/nonexistent/path"})
        assert r.status_code == 200
        assert r.json()["count"] == 0


def test_api_index_missing_path_returns_400():
    from starlette.testclient import TestClient

    from opencode_search.server.routes import build_test_app as create_app
    with TestClient(create_app()) as c:
        r = c.post("/api/index", json={"enabled": True})
        assert r.status_code == 400
        assert "error" in r.json()


@pytest.mark.slow
def test_detect_patterns_llm_frameworks():
    """P9.2: detect_patterns() derives frameworks via LLM, not _FW static dict."""
    from pathlib import Path

    from opencode_search.core.registry import list_projects
    from opencode_search.kb.patterns import detect_patterns

    astro = next(
        (p.path for p in list_projects()
         if "astro-project" in p.path and "promo" not in p.path and p.enabled),
        None,
    )
    assert astro is not None, "astro-project must be registered (run P8)"
    result = detect_patterns(Path(astro))
    assert "frameworks" in result
    assert isinstance(result["frameworks"], list)
    # astro-project has astro + react deps → LLM should name ≥1 framework
    assert len(result["frameworks"]) >= 1


def test_index_tool_e2e(tmp_path):
    """P10.4b: enabled=True creates registry entry; enabled=False removes it + index dir."""
    from pathlib import Path

    from opencode_search.core.config import index_dir
    from opencode_search.core.registry import list_projects
    from opencode_search.server.mcp import index as index_tool

    p = str(tmp_path)
    reg = json.loads(asyncio.run(index_tool(p, enabled=True)))
    assert reg["status"] in ("flagged", "already_registered")
    assert any(proj.path == p for proj in list_projects()), "Project not in registry after register"
    reg2 = json.loads(asyncio.run(index_tool(p, enabled=True)))
    assert reg2["status"] == "already_registered"
    idx = index_dir(p)
    idx.mkdir(parents=True, exist_ok=True)
    rem = json.loads(asyncio.run(index_tool(p, enabled=False)))
    assert rem["status"] in ("removed", "not_found")
    assert not any(proj.path == p for proj in list_projects()), "Project still in registry after remove"
    assert not Path(idx).exists(), "Index dir not deleted after remove"


def test_overview_all_whats_real_astro():
    """P10.4: every what= value returns parseable non-empty data on real astro-project."""
    from opencode_search.core.registry import list_projects
    from opencode_search.server.mcp import overview as overview_tool

    astro = next(
        (p.path for p in list_projects()
         if "astro-project" in p.path and "promo" not in p.path and p.enabled),
        None,
    )
    assert astro, "astro-project must be registered (run P8)"
    whats = [
        "structure", "communities", "status", "hierarchy",
        "architecture_domains", "import_cycles",
        "surprising_connections", "suggested_questions",
        "service_mesh", "feature_map", "business_rules", "process_flows",
    ]
    for what in whats:
        result = asyncio.run(overview_tool(astro, what))
        data = json.loads(result)
        assert data, f"overview(what={what!r}) returned empty dict: {result[:120]}"


def test_service_mesh_be_nonempty():
    """BE project (astro-promo-be) has gRPC services — service_mesh must detect them."""
    from opencode_search.core.registry import list_projects
    be = next(
        (p.path for p in list_projects() if "astro-promo-be" in p.path and p.enabled),
        None,
    )
    assert be, "astro-promo-be must be registered (run P8)"
    from opencode_search.server._overview import _detect_services
    svcs = _detect_services(be)
    assert svcs, "BE project must have at least one gRPC service entry"
    names = {n for s in svcs for n in s.get("services", [])}
    assert "GwpService" in names, f"GwpService not in {sorted(names)[:10]}"


def test_auto_pipeline_status_real(live_client, tmp_path):
    """P19.6: /api/auto_pipeline_status returns real enabled/pending — not canned data.

    Register an un-indexed tmp project → it must appear in pending.
    Pause sweeps → enabled must flip to False.
    """
    import urllib.request

    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.registry import remove_project, upsert_project

    proj_path = str(tmp_path)
    upsert_project(ProjectEntry(path=proj_path, enabled=True))
    try:
        r = live_client.get("/api/auto_pipeline_status")
        assert r.status_code == 200, f"unexpected status {r.status_code}"
        d = r.json()
        assert "enabled" in d and "pending" in d, f"missing keys: {d}"
        assert d["enabled"] is True, f"expected enabled=True (sweeps running), got {d}"
        assert proj_path in d["pending"], (
            f"un-indexed {proj_path} must appear in pending; got {d['pending'][:3]}"
        )

        urllib.request.urlopen(
            urllib.request.Request("http://127.0.0.1:8765/api/sweeps/pause", data=b"", method="POST"),
            timeout=3,
        )
        r2 = live_client.get("/api/auto_pipeline_status")
        d2 = r2.json()
        assert d2["enabled"] is False, f"expected enabled=False after pause, got {d2}"
    finally:
        urllib.request.urlopen(
            urllib.request.Request("http://127.0.0.1:8765/api/sweeps/resume", data=b"", method="POST"),
            timeout=3,
        )
        remove_project(proj_path)
