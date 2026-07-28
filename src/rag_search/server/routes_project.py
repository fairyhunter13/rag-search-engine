"""Storage-health and project-docs HTTP routes."""
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


_C4_PREFIXES = [
    "README.md", "01-context", "02-containers", "03-components",
    "04-reference", "05-how-to", "06-decisions", "_meta",
]


async def _api_docs(request: Request) -> JSONResponse:
    """List all .md files under <project>/docs/ in C4 order."""
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    docs_root = Path(project) / "docs"
    if not docs_root.exists():
        return JSONResponse({"tree": []})
    def _key(r: str) -> tuple:
        for i, p in enumerate(_C4_PREFIXES):
            if r == p or r.startswith(p + "/"):
                return (i, r)
        return (len(_C4_PREFIXES), r)
    tree = sorted((str(f.relative_to(docs_root)) for f in docs_root.rglob("*.md")), key=_key)
    return JSONResponse({"tree": tree})


async def _api_docs_page(request: Request) -> JSONResponse:
    """Serve one .md from <project>/docs/. Guards against path traversal."""
    project = request.query_params.get("project", "")
    rel = request.query_params.get("path", "")
    if not project or not rel:
        return JSONResponse({"error": "project and path required"}, status_code=400)
    docs_root = (Path(project) / "docs").resolve()
    target = (docs_root / rel).resolve()
    if not str(target).startswith(str(docs_root) + "/") or target.suffix.lower() != ".md":
        return JSONResponse({"error": "forbidden"}, status_code=400)
    if not target.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"path": rel, "content": target.read_text(encoding="utf-8")})


def register(app) -> None:
    app.add_route("/api/storage_health", _api_storage_health, methods=["GET"])
    app.add_route("/api/docs", _api_docs, methods=["GET"])
    app.add_route("/api/docs/page", _api_docs_page, methods=["GET"])
