"""Wiki and KB health HTTP routes."""
from __future__ import annotations

from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from opencode_search.core.config import (
    project_graph_db,
    project_vector_db,
    project_wiki_dir,
    root_process_db,
)


async def _api_wiki(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    wiki_dir = project_wiki_dir(project)
    pages = [p.stem for p in sorted(wiki_dir.glob("*.md"))] if wiki_dir.exists() else []
    return JSONResponse({"pages": pages, "project": project})


async def _api_wiki_page(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    # The dashboard sends `name`; accept either to avoid a silent 400 (page/name drift).
    page = request.query_params.get("page", "") or request.query_params.get("name", "")
    if not project or not page:
        return JSONResponse({"error": "project and page required"}, status_code=400)
    p = project_wiki_dir(project) / f"{page}.md"
    if not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"page": page, "content": p.read_text()})


async def _api_wiki_export(request: Request) -> JSONResponse:
    """Bundle the whole wiki into one artifact. ?format=markdown (default) | json.

    markdown: index first, then a table of contents, then every page concatenated under an
    anchor heading. json: {pages: [{name, content}]}. Pure string assembly over the *.md files.
    """
    project = request.query_params.get("project", "")
    fmt = request.query_params.get("format", "markdown")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    wiki_dir = project_wiki_dir(project)
    paths = sorted(wiki_dir.glob("*.md")) if wiki_dir.exists() else []
    # index first, federation second, then the rest (stable, readable order).
    order = {"index": 0, "federation": 1}
    paths.sort(key=lambda p: (order.get(p.stem, 2), p.stem))
    if fmt == "json":
        return JSONResponse({"pages": [{"name": p.stem, "content": p.read_text()} for p in paths]})
    toc = "\n".join(f"- [{p.stem}](#{p.stem})" for p in paths)
    body = "\n\n".join(f'<a id="{p.stem}"></a>\n\n{p.read_text()}' for p in paths)
    bundle = f"# Wiki Export\n\n## Contents\n\n{toc}\n\n---\n\n{body}"
    return JSONResponse({"format": "markdown", "pages": len(paths), "content": bundle})


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


def _kb_health_sync(gdb) -> dict:  # type: ignore[no-untyped-def]
    from opencode_search.graph.store import GraphStore
    gs = GraphStore(gdb)
    try:
        comms = gs.conn.execute("SELECT COUNT(*) FROM communities WHERE level = 1").fetchone()[0]
        enriched = gs.conn.execute(
            "SELECT COUNT(*) FROM communities WHERE level = 1 AND summary IS NOT NULL AND summary != ''"
        ).fetchone()[0]
        pct = (enriched / comms * 100) if comms else 0
        return {"verdict": "DONE" if pct >= 95 else "PENDING",
                "enriched_pct": round(pct, 1),
                "enriched_communities": enriched,
                "total_communities": comms}
    finally:
        gs.close()


async def _api_kb_health(request: Request) -> JSONResponse:
    import asyncio
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    gdb = project_graph_db(project)
    if not gdb.exists():
        return JSONResponse({"verdict": "PENDING", "enriched_pct": 0})
    return JSONResponse(await asyncio.to_thread(_kb_health_sync, gdb))


def _storage_health_sync(idx: Path) -> float:
    return sum(f.stat().st_size for f in idx.rglob("*") if f.is_file()) / 1_048_576 if idx.exists() else 0.0


async def _api_storage_health(request: Request) -> JSONResponse:
    import asyncio
    project = request.query_params.get("project", "")
    idx = project_vector_db(project).parent if project else Path.home() / ".local/share/opencode-search"
    mb = await asyncio.to_thread(_storage_health_sync, idx)
    return JSONResponse({"size_mb": round(mb, 1), "path": str(idx)})


def _bpmn_query_sync(pdb, process_id: str) -> str | None:  # type: ignore[no-untyped-def]
    import sqlite3 as _sq
    con = _sq.connect(str(pdb))
    try:
        row = con.execute(
            "SELECT bpmn_xml FROM process_artifacts WHERE process_id=?", (process_id,)
        ).fetchone()
    finally:
        con.close()
    return row[0] if row and row[0] else None


async def _api_process_bpmn(request: Request) -> JSONResponse:
    """Return BPMN 2.0 XML for a single process.  ?root=<root_path>&id=<process_id>"""
    import asyncio
    root = request.query_params.get("root", "")
    process_id = request.query_params.get("id", "")
    if not root or not process_id:
        return JSONResponse({"error": "root and id required"}, status_code=400)
    pdb = root_process_db(root)
    if not pdb.exists():
        return JSONResponse({"error": "process_graph.db not found — run BPRE first"}, status_code=404)
    xml = await asyncio.to_thread(_bpmn_query_sync, pdb, process_id)
    if not xml:
        return JSONResponse({"error": "process not found"}, status_code=404)
    from starlette.responses import Response
    return Response(xml, media_type="application/xml")


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
    app.add_route("/api/wiki", _api_wiki, methods=["GET"])
    app.add_route("/api/wiki/page", _api_wiki_page, methods=["GET"])
    app.add_route("/api/wiki/export", _api_wiki_export, methods=["GET"])
    app.add_route("/api/wiki_lint", _api_wiki_lint, methods=["GET"])
    app.add_route("/api/kb_health", _api_kb_health, methods=["GET"])
    app.add_route("/api/storage_health", _api_storage_health, methods=["GET"])
    app.add_route("/api/process/bpmn", _api_process_bpmn, methods=["GET"])
    app.add_route("/api/docs", _api_docs, methods=["GET"])
    app.add_route("/api/docs/page", _api_docs_page, methods=["GET"])
