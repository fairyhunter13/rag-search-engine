"""Graph analysis HTTP routes."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from opencode_search.core.config import project_graph_db


def _open_graph(project: str):
    gdb = project_graph_db(project)
    if not gdb.exists():
        return None
    from opencode_search.graph.store import GraphStore
    return GraphStore(gdb)


async def _api_graph(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    symbol = request.query_params.get("symbol", "")
    relation = request.query_params.get("relation", "definition")
    if not project or not symbol:
        return JSONResponse({"error": "project and symbol required"}, status_code=400)
    gs = _open_graph(project)
    if gs is None:
        return JSONResponse({"error": "project not indexed"}, status_code=404)
    from opencode_search.query.graph_handler import (
        callees,
        callers,
        definition,
        impact,
        impact_narrative,
    )
    fn = {"callers": callers, "callees": callees, "impact": impact,
          "impact_narrative": impact_narrative}.get(relation, definition)
    try:
        return JSONResponse({"results": fn(symbol, gs), "relation": relation})
    finally:
        gs.close()


async def _api_impact_narrative(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    symbol = request.query_params.get("symbol", "")
    if not project or not symbol:
        return JSONResponse({"error": "project and symbol required"}, status_code=400)
    gs = _open_graph(project)
    if gs is None:
        return JSONResponse({"error": "not indexed"}, status_code=404)
    from opencode_search.query.graph_handler import impact_narrative
    try:
        return JSONResponse({"narrative": impact_narrative(symbol, gs)})
    finally:
        gs.close()


async def _api_graph_export(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    max_nodes = int(request.query_params.get("max_nodes", "5000"))
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    gs = _open_graph(project)
    if gs is None:
        return JSONResponse({"nodes": [], "edges": []})
    try:
        nodes = gs.conn.execute("SELECT id, name, kind FROM symbols LIMIT ?", (max_nodes,)).fetchall()
        edges = gs.conn.execute("SELECT source_id, target_id, kind FROM edges LIMIT ?", (max_nodes,)).fetchall()
        return JSONResponse({"nodes": [dict(n) for n in nodes], "edges": [dict(e) for e in edges]})
    finally:
        gs.close()


async def _api_import_cycles(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    gs = _open_graph(project)
    if gs is None:
        return JSONResponse({"cycles": []})
    try:
        cnt = gs.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        return JSONResponse({"cycles": [], "import_edge_count": cnt})
    finally:
        gs.close()


async def _api_graph_diff(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    gs = _open_graph(project)
    if gs is None:
        return JSONResponse({"added": [], "removed": []})
    try:
        rows = gs.conn.execute(
            "SELECT name, kind FROM symbols ORDER BY rowid DESC LIMIT 50"
        ).fetchall()
        return JSONResponse({"added": [{"name": r[0], "kind": r[1]} for r in rows], "removed": []})
    finally:
        gs.close()


async def _api_surprising_connections(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    gs = _open_graph(project)
    if gs is None:
        return JSONResponse({"connections": []})
    try:
        rows = gs.conn.execute(
            "SELECT s.name as src, t.name as tgt FROM edges e "
            "JOIN symbols s ON e.source_id=s.id JOIN symbols t ON e.target_id=t.id "
            "WHERE s.community_id != t.community_id LIMIT 20"
        ).fetchall()
        return JSONResponse({"connections": [dict(r) for r in rows]})
    finally:
        gs.close()


async def _api_service_mesh(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    return JSONResponse({"services": [], "project": project})


async def _api_semantic_trace(request: Request) -> JSONResponse:
    from_ = request.query_params.get("from", "")
    to = request.query_params.get("to", "")
    if not from_ or not to:
        return JSONResponse({"error": "from and to required"}, status_code=400)
    return JSONResponse({"trace": f"{from_} → {to}", "steps": []})


async def _api_callflow_html(request: Request) -> HTMLResponse:
    symbol = request.query_params.get("symbol", "")
    if not symbol:
        return HTMLResponse("<p>symbol required</p>", status_code=400)
    return HTMLResponse(f"<pre>callflow: {symbol}</pre>")


def register(app) -> None:
    app.add_route("/api/graph", _api_graph, methods=["GET"])
    app.add_route("/api/impact_narrative", _api_impact_narrative, methods=["GET"])
    app.add_route("/api/graph_export", _api_graph_export, methods=["GET"])
    app.add_route("/api/import_cycles", _api_import_cycles, methods=["GET"])
    app.add_route("/api/graph_diff", _api_graph_diff, methods=["GET"])
    app.add_route("/api/surprising_connections", _api_surprising_connections, methods=["GET"])
    app.add_route("/api/service_mesh", _api_service_mesh, methods=["GET"])
    app.add_route("/api/semantic_trace", _api_semantic_trace, methods=["GET"])
    app.add_route("/api/callflow_html", _api_callflow_html, methods=["GET"])
