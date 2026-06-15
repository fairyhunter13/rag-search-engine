"""Pipeline, enrichment, jobs, and federation routes."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from opencode_search.core.config import project_graph_db


async def _api_build_hierarchy(request: Request) -> JSONResponse:
    project_path = request.query_params.get("project", "")
    action = request.query_params.get("action", "hierarchy")
    if not project_path:
        try:
            body = await request.json()
            project_path = body.get("project_path", "")
            action = body.get("action", action)
        except Exception:
            pass
    if not project_path:
        return JSONResponse({"error": "project required"}, status_code=400)
    from opencode_search.core.config import project_wiki_dir
    from opencode_search.graph.store import GraphStore
    from opencode_search.kb.hierarchy import build_hierarchy
    from opencode_search.kb.wiki import build_wiki
    gdb = project_graph_db(project_path)
    if not gdb.exists():
        return JSONResponse({"error": "not indexed"}, status_code=404)
    gs = GraphStore(gdb)
    try:
        if action == "wiki":
            n = build_wiki(gs, project_wiki_dir(project_path))
            return JSONResponse({"status": "ok", "pages_written": n})
        n = build_hierarchy(gs)
        return JSONResponse({"status": "ok", "communities_built": n})
    finally:
        gs.close()


async def _api_auto_pipeline_status(request: Request) -> JSONResponse:
    import sqlite3

    from opencode_search.core.registry import list_projects
    from opencode_search.daemon import sweeps
    pending = []
    for p in list_projects():
        if not p.enabled:
            continue
        if sweeps._needs_index(p.path):
            pending.append(p.path)
            continue
        gdb = project_graph_db(p.path)
        if gdb.exists():
            try:
                with sqlite3.connect(str(gdb)) as con:
                    n = con.execute("SELECT COUNT(*) FROM communities WHERE (summary IS NULL OR summary = '') AND level = 1").fetchone()[0]
                    if n:
                        pending.append(p.path)
            except Exception:
                pass
    return JSONResponse({"enabled": not sweeps._PAUSED, "pending": pending})


async def _api_federation(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    from opencode_search.core.registry import list_projects
    members = [p.path for p in list_projects() if p.path != project and p.enabled]
    return JSONResponse({"root": project, "members": members})


def register(app) -> None:
    app.add_route("/api/build_hierarchy", _api_build_hierarchy, methods=["POST"])
    app.add_route("/api/auto_pipeline_status", _api_auto_pipeline_status, methods=["GET"])
    app.add_route("/api/federation", _api_federation, methods=["GET"])
