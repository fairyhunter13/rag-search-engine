"""HTTP route handlers + create_app() for the dashboard API."""
from __future__ import annotations

import os
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.staticfiles import StaticFiles

_STATIC_DIR = Path(__file__).parent / "static"
_START = time.monotonic()


async def _healthz(request: Request) -> JSONResponse:
    import psutil

    from rag_search.daemon.cpu_budget import cpu_percent_core, cpu_quota_cores
    from rag_search.daemon.runtime_state import seconds_since_activity
    la = psutil.getloadavg()
    return JSONResponse({
        "ok": True, "service": "rag-search", "transport": "streamable-http",
        "uptime_s": round(time.monotonic() - _START, 1),
        "load_avg": {"1m": la[0], "5m": la[1], "15m": la[2]},
        "cpu_count": os.cpu_count() or 1,
        "active_clients": 0, "client_ids": [], "active_projects": [],
        "closing_clients": [], "idle_seconds": round(seconds_since_activity(), 1),
        "rss_mb": round(psutil.Process().memory_info().rss / (1024 * 1024)),
        "cpu_percent_core": round(cpu_percent_core(), 4),
        "cpu_quota_cores": cpu_quota_cores(),
    })


async def _dashboard(request: Request) -> HTMLResponse:
    from rag_search.server.dashboard import html
    return HTMLResponse(html())


async def _api_projects(request: Request) -> JSONResponse:
    from rag_search.core.registry import list_projects
    return JSONResponse({"projects": [
        {"path": p.path, "enabled": p.enabled, "indexed_at": p.indexed_at}
        for p in list_projects()
    ]})


async def _api_overview(request: Request) -> JSONResponse:
    import asyncio
    import json
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    from rag_search.server._overview import handle_overview
    proj = body.get("project") or body.get("project_path", "")
    what = body.get("what", "structure")
    return JSONResponse(json.loads(await asyncio.to_thread(handle_overview, proj, what)))


def _register_all(app) -> None:
    from rag_search.server import (
        routes_admin,
        routes_chat,
        routes_graph,
        routes_ops,
        routes_pipeline,
        routes_project,
        routes_search,
    )
    for mod in (routes_admin, routes_project, routes_search, routes_graph,
                routes_ops, routes_pipeline, routes_chat):
        mod.register(app)


def create_app():
    """Build Starlette app: FastMCP streamable-HTTP + dashboard API routes."""
    from rag_search.server.mcp import mcp
    app = mcp.streamable_http_app()
    app.add_route("/healthz", _healthz, methods=["GET"])
    app.add_route("/dashboard", _dashboard, methods=["GET"])
    app.add_route("/api/projects", _api_projects, methods=["GET"])
    app.add_route("/api/overview", _api_overview, methods=["POST"])
    _register_all(app)
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    return app
