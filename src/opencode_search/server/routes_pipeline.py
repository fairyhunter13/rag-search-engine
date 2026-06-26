"""Pipeline, enrichment, jobs, and federation routes."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from opencode_search.core.config import project_graph_db


async def _api_build_wiki(request: Request) -> JSONResponse:
    project_path = request.query_params.get("project", "")
    action = request.query_params.get("action", "wiki")
    if not project_path:
        try:
            body = await request.json()
            project_path = body.get("project_path", "")
            action = body.get("action", action)
        except Exception:
            pass
    if not project_path:
        return JSONResponse({"error": "project required"}, status_code=400)
    import time as _time

    from opencode_search.core.config import project_wiki_dir
    from opencode_search.graph.store import GraphStore
    from opencode_search.kb.wiki import build_wiki
    from opencode_search.server.routes_ops import publish_event
    gdb = project_graph_db(project_path)
    if not gdb.exists():
        return JSONResponse({"error": "not indexed"}, status_code=404)
    if action != "wiki":
        return JSONResponse({"error": "only action=wiki is supported"}, status_code=400)
    job_id = str(int(_time.time()))
    gs = GraphStore(gdb)
    try:
        n = build_wiki(gs, project_wiki_dir(project_path))
        publish_event({"type": "job", "job_id": job_id, "action": "wiki", "status": "done"})
        return JSONResponse({"status": "ok", "pages_written": n})
    except Exception:
        publish_event({"type": "job", "job_id": job_id, "action": action, "status": "error"})
        raise
    finally:
        gs.close()


async def _api_auto_pipeline_status(request: Request) -> JSONResponse:
    from opencode_search.core.registry import list_projects
    from opencode_search.daemon import sweeps
    pending = []
    for p in list_projects():
        if not p.enabled:
            continue
        if sweeps._needs_index(p.path) or sweeps._needs_enrich(p.path):
            pending.append(p.path)
    return JSONResponse({"enabled": not sweeps._PAUSED, "pending": pending})


async def _api_docgen(request: Request) -> JSONResponse:
    project_path = ""
    try:
        body = await request.json()
        project_path = body.get("project_path", "")
    except Exception:
        pass
    if not project_path:
        return JSONResponse({"error": "project_path required"}, status_code=400)
    import asyncio

    from opencode_search.kb.docgen import run_docgen
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, run_docgen, project_path)
    return JSONResponse({"status": "ok", "project": project_path})


async def _api_okf(request: Request) -> JSONResponse:
    project_path = ""
    try:
        body = await request.json()
        project_path = body.get("project_path", "")
    except Exception:
        pass
    if not project_path:
        return JSONResponse({"error": "project_path required"}, status_code=400)
    import asyncio

    from opencode_search.kb.okf import run_okf
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, run_okf, project_path)
    return JSONResponse({"status": "ok", "project": project_path, **result})


def register(app) -> None:
    app.add_route("/api/build_wiki", _api_build_wiki, methods=["POST"])
    app.add_route("/api/auto_pipeline_status", _api_auto_pipeline_status, methods=["GET"])
    app.add_route("/api/docgen", _api_docgen, methods=["POST"])
    app.add_route("/api/okf", _api_okf, methods=["POST"])
