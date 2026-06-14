"""Pipeline, enrichment, jobs, and federation routes."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from opencode_search.core.config import project_graph_db


async def _api_build_hierarchy(request: Request) -> JSONResponse:
    body = await request.json()
    project_path = body.get("project_path", "")
    if not project_path:
        return JSONResponse({"error": "project_path required"}, status_code=400)
    from opencode_search.graph.store import GraphStore
    from opencode_search.kb.hierarchy import build_hierarchy
    gdb = project_graph_db(project_path)
    if not gdb.exists():
        return JSONResponse({"error": "not indexed"}, status_code=404)
    gs = GraphStore(gdb)
    try:
        n = build_hierarchy(gs)
        return JSONResponse({"status": "ok", "communities_built": n})
    finally:
        gs.close()


async def _api_enrich_hierarchy(request: Request) -> JSONResponse:
    body = await request.json()
    project_path = body.get("project_path", "")
    if not project_path:
        return JSONResponse({"error": "project_path required"}, status_code=400)
    return JSONResponse({"status": "enqueued", "project": project_path})


async def _api_enrich_project(request: Request) -> JSONResponse:
    body = await request.json()
    project_path = body.get("project_path", "")
    if not project_path:
        return JSONResponse({"error": "project_path required"}, status_code=400)
    return JSONResponse({"status": "enqueued", "project": project_path})


async def _api_auto_pipeline_status(request: Request) -> JSONResponse:
    return JSONResponse({"enabled": True, "pending": []})


async def _api_federation(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    from opencode_search.core.registry import list_projects
    members = [p.path for p in list_projects() if p.path != project and p.enabled]
    return JSONResponse({"root": project, "members": members})


async def _api_jobs(request: Request) -> JSONResponse:
    return JSONResponse({"jobs": []})


async def _api_job_get(request: Request) -> JSONResponse:
    return JSONResponse({"job_id": request.path_params.get("job_id"), "status": "unknown"})


async def _api_job_cancel(request: Request) -> JSONResponse:
    return JSONResponse({"job_id": request.path_params.get("job_id"), "status": "cancelled"})


def register(app) -> None:
    app.add_route("/api/build_hierarchy", _api_build_hierarchy, methods=["POST"])
    app.add_route("/api/enrich_hierarchy", _api_enrich_hierarchy, methods=["POST"])
    app.add_route("/api/enrich_project", _api_enrich_project, methods=["POST"])
    app.add_route("/api/auto_pipeline_status", _api_auto_pipeline_status, methods=["GET"])
    app.add_route("/api/federation", _api_federation, methods=["GET"])
    app.add_route("/api/jobs", _api_jobs, methods=["GET"])
    app.add_route("/api/jobs/{job_id}", _api_job_get, methods=["GET"])
    app.add_route("/api/jobs/{job_id}/cancel", _api_job_cancel, methods=["POST"])
