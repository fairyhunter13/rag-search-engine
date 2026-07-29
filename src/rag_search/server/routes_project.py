"""Storage-health HTTP route.

The project-docs browser (`/api/docs`, `/api/docs/page`) was the other half of this module and
left 2026-07-29 with the dashboard's Docs pane. It listed and served `<project>/docs/*.md` in C4
order so the pane could render them — a markdown reader, in a browser tab, for files already on
disk. The dashboard is an operator console now, and reading a repo's own markdown is what the
editor and Claude Code are for.
"""
from __future__ import annotations

from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from rag_search.core.config import project_vector_db


def _storage_health_sync(idx: Path) -> float:
    return sum(f.stat().st_size for f in idx.rglob("*") if f.is_file()) / 1_048_576 if idx.exists() else 0.0


async def _api_storage_health(request: Request) -> JSONResponse:
    import asyncio
    project = request.query_params.get("project", "")
    idx = project_vector_db(project).parent if project else Path.home() / ".local/share/rag-search"
    mb = await asyncio.to_thread(_storage_health_sync, idx)
    return JSONResponse({"size_mb": round(mb, 1), "path": str(idx)})


def register(app) -> None:
    app.add_route("/api/storage_health", _api_storage_health, methods=["GET"])
