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
    """overview(what='projects') returns a list of registered projects."""
    from opencode_search.server.mcp import overview as overview_tool
    result = asyncio.run(overview_tool("", "projects"))
    data = json.loads(result)
    assert "projects" in data
    assert isinstance(data["projects"], list)


def test_mcp_index_register_remove(tmp_path):
    """index tool registers then removes a project without crashing."""
    from opencode_search.server.mcp import index as index_tool
    p = str(tmp_path)
    reg = json.loads(asyncio.run(index_tool(p, enabled=True)))
    assert reg["status"] in ("flagged", "already_registered")
    rem = json.loads(asyncio.run(index_tool(p, enabled=False)))
    assert rem["status"] in ("removed", "not_found")


def test_healthz():
    from starlette.testclient import TestClient

    from opencode_search.server.routes import build_test_app as create_app
    with TestClient(create_app()) as c:
        r = c.get("/healthz")
        assert r.status_code == 200
        assert r.json()["ok"] is True


def test_dashboard_five_views():
    from starlette.testclient import TestClient

    from opencode_search.server.routes import build_test_app as create_app
    with TestClient(create_app()) as c:
        r = c.get("/dashboard")
        assert r.status_code == 200
        body = r.text.lower()
        for view in ("pulse", "chat", "admin", "wiki", "graph"):
            assert view in body, f"dashboard missing '{view}' view"


def test_api_projects_returns_list():
    from starlette.testclient import TestClient

    from opencode_search.server.routes import build_test_app as create_app
    with TestClient(create_app()) as c:
        r = c.get("/api/projects")
        assert r.status_code == 200
        data = r.json()
        assert "projects" in data
        assert isinstance(data["projects"], list)


def test_api_overview_projects():
    from starlette.testclient import TestClient

    from opencode_search.server.routes import build_test_app as create_app
    with TestClient(create_app()) as c:
        r = c.post("/api/overview", json={"what": "projects"})
        assert r.status_code == 200
        assert "projects" in r.json()


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
