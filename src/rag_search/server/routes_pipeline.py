"""Auto-pipeline status route."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse


async def _api_auto_pipeline_status(request: Request) -> JSONResponse:
    from rag_search.core.registry import list_projects
    from rag_search.daemon import sweeps
    pending = []
    for p in list_projects():
        if not p.enabled:
            continue
        if sweeps._needs_index(p.path) or sweeps._needs_labels(p.path):
            pending.append(p.path)
    # `is_paused()`, not the raw flag: this route is what an operator polls to ask whether the
    # pipeline is running, so reading round the lease would report "disabled" for a pause that has
    # already expired and been resumed — and, worse, would never trigger the expiry itself.
    return JSONResponse({"enabled": not sweeps.is_paused(), "pending": pending})


def register(app) -> None:
    app.add_route("/api/auto_pipeline_status", _api_auto_pipeline_status, methods=["GET"])
