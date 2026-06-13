"""HTTP route handlers + create_app() for the dashboard API."""
from __future__ import annotations

from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.staticfiles import StaticFiles

_STATIC_DIR = Path(__file__).parent / "static"


async def _healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def _dashboard(request: Request) -> HTMLResponse:
    from opencode_search.server.dashboard import html
    return HTMLResponse(html())


async def _api_projects(request: Request) -> JSONResponse:
    from opencode_search.core.registry import list_projects
    return JSONResponse({"projects": [
        {"path": p.path, "enabled": p.enabled, "indexed_at": p.indexed_at}
        for p in list_projects()
    ]})


async def _api_search(request: Request) -> JSONResponse:
    body = await request.json()
    q = body.get("q") or body.get("query", "")
    if not q:
        return JSONResponse({"error": "q required"}, status_code=400)
    project = body.get("project", "")
    scope = body.get("scope", "code")
    from opencode_search.core.config import project_vector_db
    from opencode_search.core.registry import list_projects
    from opencode_search.index.store import VectorStore
    from opencode_search.query.search import search as _search
    from opencode_search.server.mcp import _get_embedder
    paths = [project] if project else [p.path for p in list_projects() if p.enabled]
    results: list[dict] = []
    for path in paths:
        vdb = project_vector_db(path)
        if not vdb.exists():
            continue
        vs = VectorStore(vdb)
        try:
            results.extend(_search(q, _get_embedder(), vs, scope=scope, top_k=10))
        finally:
            vs.close()
    results.sort(key=lambda r: r.get("score", 0), reverse=True)
    return JSONResponse({"results": results[:10], "count": len(results)})


async def _api_ask(request: Request) -> JSONResponse:
    body = await request.json()
    q = body.get("q") or body.get("query", "")
    if not q:
        return JSONResponse({"error": "query required"}, status_code=400)
    from opencode_search.server import mcp as mcp_mod
    answer = await mcp_mod.ask(q, body.get("project", ""), body.get("scope", "all"))
    return JSONResponse({"answer": answer})


async def _api_overview(request: Request) -> JSONResponse:
    import json
    body = await request.json()
    from opencode_search.server._overview import handle_overview
    return JSONResponse(json.loads(handle_overview(body.get("project", ""), body.get("what", "structure"))))


async def _api_index(request: Request) -> JSONResponse:
    body = await request.json()
    path = body.get("path", "")
    if not path:
        return JSONResponse({"error": "path required"}, status_code=400)
    if not body.get("enabled", True):
        from opencode_search.core.registry import remove_project
        return JSONResponse({"removed": remove_project(path)})
    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.registry import upsert_project
    upsert_project(ProjectEntry(path=path, enabled=True))
    return JSONResponse({"registered": True, "path": path})


def _register_all(app) -> None:
    from opencode_search.server import (
        routes_admin,
        routes_chat,
        routes_graph,
        routes_ops,
        routes_pipeline,
        routes_project,
        routes_search,
        routes_semantic,
    )
    for mod in (routes_admin, routes_project, routes_search, routes_graph,
                routes_semantic, routes_ops, routes_pipeline, routes_chat):
        mod.register(app)


def create_app():
    """Build Starlette app: FastMCP streamable-HTTP + dashboard API routes."""
    from opencode_search.server.mcp import mcp
    app = mcp.streamable_http_app()
    app.add_route("/healthz", _healthz, methods=["GET"])
    app.add_route("/dashboard", _dashboard, methods=["GET"])
    app.add_route("/api/projects", _api_projects, methods=["GET"])
    app.add_route("/api/search", _api_search, methods=["POST"])
    app.add_route("/api/ask", _api_ask, methods=["POST"])
    app.add_route("/api/overview", _api_overview, methods=["POST"])
    app.add_route("/api/index", _api_index, methods=["POST"])
    _register_all(app)
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    return app


def build_test_app():
    """Plain Starlette app for testing — no FastMCP transport (session manager is single-use)."""
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    app = Starlette(routes=[
        Route("/healthz", _healthz, methods=["GET"]),
        Route("/dashboard", _dashboard, methods=["GET"]),
        Route("/api/projects", _api_projects, methods=["GET"]),
        Route("/api/search", _api_search, methods=["POST"]),
        Route("/api/ask", _api_ask, methods=["POST"]),
        Route("/api/overview", _api_overview, methods=["POST"]),
        Route("/api/index", _api_index, methods=["POST"]),
        Mount("/static", StaticFiles(directory=_STATIC_DIR), name="static"),
    ])
    _register_all(app)
    return app
