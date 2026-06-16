"""Graph analysis HTTP routes."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse


async def _api_graph(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    symbol = request.query_params.get("symbol", "")
    relation = request.query_params.get("relation", "definition")
    if not project or not symbol:
        return JSONResponse({"error": "project and symbol required"}, status_code=400)
    from opencode_search.daemon.federation import federated_map
    from opencode_search.query.graph_handler import (
        callees,
        callers,
        definition,
        impact,
        impact_narrative,
    )
    _fn = {"callers": callers, "callees": callees, "impact": impact,
           "impact_narrative": impact_narrative}.get(relation, definition)
    results = [m for _, ms in federated_map(project, lambda gs: _fn(symbol, gs)) for m in ms]
    return JSONResponse({"results": results, "relation": relation})


async def _api_impact_narrative(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    symbol = request.query_params.get("symbol", "")
    if not project or not symbol:
        return JSONResponse({"error": "project and symbol required"}, status_code=400)
    from opencode_search.daemon.federation import federated_map
    from opencode_search.query.graph_handler import impact_narrative
    narrative = " ".join(n for _, n in federated_map(project, lambda gs: impact_narrative(symbol, gs)) if n)
    return JSONResponse({"narrative": narrative or f"No callers found for '{symbol}' — low blast radius."})


async def _api_graph_export(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    max_nodes = int(request.query_params.get("max_nodes", "5000"))
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    from opencode_search.daemon.federation import federated_map
    def _export(gs):  # type: ignore[no-untyped-def]
        n = [dict(r) for r in gs.conn.execute("SELECT sid AS id, name, kind FROM symbols LIMIT ?", (max_nodes,)).fetchall()]
        e = [dict(r) for r in gs.conn.execute("SELECT caller_sid AS source_id, callee_sid AS target_id FROM edges LIMIT ?", (max_nodes,)).fetchall()]
        return n, e
    all_n, all_e = [], []
    for _, (ns, es) in federated_map(project, _export):
        all_n.extend(ns)
        all_e.extend(es)
    return JSONResponse({"nodes": all_n[:max_nodes], "edges": all_e[:max_nodes]})


async def _api_import_cycles(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    from opencode_search.daemon.federation import federated_map
    cnt = sum(c for _, c in federated_map(project, lambda gs: gs.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]))
    return JSONResponse({"cycles": [], "import_edge_count": cnt})


async def _api_surprising_connections(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    from opencode_search.daemon.federation import federated_map
    rows = [r for _, rs in federated_map(project, lambda gs: [dict(r) for r in gs.conn.execute(
        "SELECT s.name as src, t.name as tgt FROM edges e "
        "JOIN symbols s ON e.caller_sid=s.sid JOIN symbols t ON e.callee_sid=t.sid "
        "WHERE s.community_id != t.community_id LIMIT 20"
    ).fetchall()]) for r in rs]
    return JSONResponse({"connections": rows[:20]})


async def _api_service_mesh(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    from opencode_search.daemon.federation import expand_federation
    from opencode_search.server._overview import _detect_services
    return JSONResponse({"services": [s for p in expand_federation(project) for s in _detect_services(p)]})


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
    app.add_route("/api/surprising_connections", _api_surprising_connections, methods=["GET"])
    app.add_route("/api/service_mesh", _api_service_mesh, methods=["GET"])
    app.add_route("/api/semantic_trace", _api_semantic_trace, methods=["GET"])
    app.add_route("/api/callflow_html", _api_callflow_html, methods=["GET"])
