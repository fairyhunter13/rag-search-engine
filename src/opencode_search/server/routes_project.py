"""Wiki and KB health HTTP routes."""
from __future__ import annotations

from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from opencode_search.core.config import project_graph_db, project_vector_db, project_wiki_dir


async def _api_wiki(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    wiki_dir = project_wiki_dir(project)
    pages = [p.stem for p in sorted(wiki_dir.glob("*.md"))] if wiki_dir.exists() else []
    return JSONResponse({"pages": pages, "project": project})


async def _api_wiki_page(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    page = request.query_params.get("page", "")
    if not project or not page:
        return JSONResponse({"error": "project and page required"}, status_code=400)
    p = project_wiki_dir(project) / f"{page}.md"
    if not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"page": page, "content": p.read_text()})


async def _api_wiki_lint(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    wiki_dir = project_wiki_dir(project)
    issues = [
        {"page": p.stem, "issue": "too short"}
        for p in wiki_dir.glob("*.md")
        if wiki_dir.exists() and len(p.read_text().strip()) < 20
    ]
    return JSONResponse({"issues": issues})


async def _api_kb_health(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    gdb = project_graph_db(project)
    if not gdb.exists():
        return JSONResponse({"verdict": "PENDING", "enriched_pct": 0})
    from opencode_search.graph.store import GraphStore
    gs = GraphStore(gdb)
    try:
        comms = gs.conn.execute("SELECT COUNT(*) FROM communities WHERE level = 1").fetchone()[0]
        enriched = gs.conn.execute(
            "SELECT COUNT(*) FROM communities WHERE level = 1 AND summary IS NOT NULL AND summary != ''"
        ).fetchone()[0]
        pct = (enriched / comms * 100) if comms else 0
        return JSONResponse({"verdict": "DONE" if pct >= 95 else "PENDING",
                             "enriched_pct": round(pct, 1),
                             "enriched_communities": enriched,
                             "total_communities": comms})
    finally:
        gs.close()


async def _api_storage_health(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    idx = project_vector_db(project).parent if project else Path.home() / ".local/share/opencode-search"
    mb = sum(f.stat().st_size for f in idx.rglob("*") if f.is_file()) / 1_048_576 if idx.exists() else 0
    return JSONResponse({"size_mb": round(mb, 1), "path": str(idx)})


def register(app) -> None:
    app.add_route("/api/wiki", _api_wiki, methods=["GET"])
    app.add_route("/api/wiki/page", _api_wiki_page, methods=["GET"])
    app.add_route("/api/wiki_lint", _api_wiki_lint, methods=["GET"])
    app.add_route("/api/kb_health", _api_kb_health, methods=["GET"])
    app.add_route("/api/storage_health", _api_storage_health, methods=["GET"])
