"""Business/semantic classification HTTP routes."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from opencode_search.core.config import project_graph_db, project_vector_db


def _open_graph(project: str):
    gdb = project_graph_db(project)
    if not gdb.exists():
        return None
    from opencode_search.graph.store import GraphStore
    return GraphStore(gdb)


async def _api_feature_map(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    gs = _open_graph(project)
    if gs is None:
        return JSONResponse({"features": []})
    try:
        rows = gs.conn.execute(
            "SELECT label, semantic_type FROM communities WHERE semantic_type='feature' LIMIT 50"
        ).fetchall()
        return JSONResponse({"features": [dict(r) for r in rows]})
    finally:
        gs.close()


async def _api_business_rules(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    gs = _open_graph(project)
    if gs is None:
        return JSONResponse({"rules": []})
    try:
        rows = gs.conn.execute(
            "SELECT label FROM communities WHERE semantic_type='constraint' LIMIT 50"
        ).fetchall()
        return JSONResponse({"rules": [r["label"] for r in rows]})
    finally:
        gs.close()


async def _api_process_flows(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    gs = _open_graph(project)
    if gs is None:
        return JSONResponse({"flows": []})
    try:
        rows = gs.conn.execute(
            "SELECT label FROM communities WHERE semantic_type='workflow' LIMIT 50"
        ).fetchall()
        return JSONResponse({"flows": [r["label"] for r in rows]})
    finally:
        gs.close()


async def _api_ask_business(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    query = request.query_params.get("query", "")
    if not query:
        return JSONResponse({"error": "query required"}, status_code=400)
    from opencode_search.graph.store import GraphStore
    from opencode_search.query.ask import ask
    gdb = project_graph_db(project) if project else None
    if not gdb or not gdb.exists():
        return JSONResponse({"answer": "Project not indexed."})
    chunks: list[dict] = []
    vdb = project_vector_db(project)
    if vdb.exists():
        from opencode_search.embed.embedder import Embedder
        from opencode_search.index.store import VectorStore
        from opencode_search.query.search import search as _search
        emb = Embedder()
        emb.warmup()
        vs = VectorStore(vdb)
        try:
            chunks = _search(query, emb, vs)
        finally:
            vs.close()
    gs = GraphStore(gdb)
    try:
        return JSONResponse({"answer": ask(query, chunks, gs, scope="all")})
    finally:
        gs.close()


def register(app) -> None:
    app.add_route("/api/feature_map", _api_feature_map, methods=["GET"])
    app.add_route("/api/business_rules", _api_business_rules, methods=["GET"])
    app.add_route("/api/process_flows", _api_process_flows, methods=["GET"])
    app.add_route("/api/ask_business", _api_ask_business, methods=["GET"])
