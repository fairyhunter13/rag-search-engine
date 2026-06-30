"""Graph export route."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse


def _graph_export_sync(project: str, max_nodes: int) -> dict:
    from opencode_search.daemon.federation import federated_map
    def _export(gs):  # type: ignore[no-untyped-def]
        n = [dict(r) for r in gs.conn.execute("SELECT sid AS id, name, kind FROM symbols LIMIT ?", (max_nodes,)).fetchall()]
        e = [dict(r) for r in gs.conn.execute("SELECT caller_sid AS source_id, callee_sid AS target_id FROM edges LIMIT ?", (max_nodes,)).fetchall()]
        return n, e
    all_n, all_e = [], []
    for _, (ns, es) in federated_map(project, _export):
        all_n.extend(ns)
        all_e.extend(es)
    return {"nodes": all_n[:max_nodes], "edges": all_e[:max_nodes]}


async def _api_graph_export(request: Request) -> JSONResponse:
    import asyncio
    project = request.query_params.get("project", "")
    max_nodes = int(request.query_params.get("max_nodes", "5000"))
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    return JSONResponse(await asyncio.to_thread(_graph_export_sync, project, max_nodes))


def register(app) -> None:
    app.add_route("/api/graph_export", _api_graph_export, methods=["GET"])
