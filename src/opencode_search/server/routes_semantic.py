"""Business/semantic classification HTTP routes."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from opencode_search.core.config import project_graph_db


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
    gs = GraphStore(gdb)
    try:
        return JSONResponse({"answer": ask(query, gs, scope="all")})
    finally:
        gs.close()


async def _api_symbol_intent(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    symbol = request.query_params.get("symbol", "")
    if not project or not symbol:
        return JSONResponse({"error": "project and symbol required"}, status_code=400)
    gs = _open_graph(project)
    if gs is None:
        return JSONResponse({"error": "not indexed"}, status_code=404)
    try:
        row = gs.conn.execute(
            "SELECT intent FROM symbols WHERE name=? LIMIT 1", (symbol,)
        ).fetchone()
        return JSONResponse({"symbol": symbol, "intent": row["intent"] if row else None})
    finally:
        gs.close()


def register(app) -> None:
    app.add_route("/api/feature_map", _api_feature_map, methods=["GET"])
    app.add_route("/api/business_rules", _api_business_rules, methods=["GET"])
    app.add_route("/api/process_flows", _api_process_flows, methods=["GET"])
    app.add_route("/api/ask_business", _api_ask_business, methods=["GET"])
    app.add_route("/api/symbol_intent", _api_symbol_intent, methods=["GET"])
