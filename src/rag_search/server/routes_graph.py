"""Graph export route."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse


def _graph_export_sync(project: str, max_nodes: int) -> dict:
    """Up to `max_nodes` symbols and the call edges *induced* by them.

    Edges are chosen first and the nodes follow, because that is the only order in which the
    two stay consistent. This read `symbols LIMIT ?` and `edges LIMIT ?` as two independent
    queries, so nothing tied an exported edge's endpoints to the exported node set. Measured
    2026-08-01 at the `max_nodes=2000` the dashboard sends: **51.3% of exported edges dangled
    fleet-wide, and 100% on a 136-member federation** — there the route collected 133,087 nodes
    and 59,492 edges and then truncated each list to 2,000 separately, so the surviving nodes
    came from the first member and the surviving edges from wherever the concatenation reached.
    Not one edge connected two exported nodes. A graph view cannot draw an edge to a node it
    was never sent, and `ORDER BY` alone would only have made the disjointness reproducible.

    Edge-first also spends the budget better: 88,299 usable edges against the old scheme's
    34,546, from 41% of the node slots, because the budget goes to the connected part of the
    graph instead of an arbitrary prefix of `symbols`.

    The budget is global rather than per member: `federated_map` runs this once per store in
    order, and the closure carries what is left. Members are deduplicated by `sid` and by
    `(caller, callee)` because `expand_federation` returns the root *and* its members, and a
    root whose directory contains its members indexes their files too. `symbol_id` hashes the
    **absolute** path (`file:name:start_line`), so an equal sid is the same symbol in the same
    file seen through two stores — never two different symbols that collided. Dedup is
    therefore a union, not a merge, and it cannot manufacture a cross-repo edge. Emitting both
    copies is what a federation root did before: 194 rows for 98 symbols.
    """
    from rag_search.daemon.federation import federated_map

    nodes: list[dict] = []
    edges: list[dict] = []
    kept: set[str] = set()
    seen_edges: set[tuple[str, str]] = set()
    fill: dict[str, dict] = {}

    def _export(gs) -> None:  # type: ignore[no-untyped-def]
        want: set[str] = set()
        for a, b in gs.conn.execute(
                "SELECT caller_sid, callee_sid FROM edges ORDER BY caller_sid, callee_sid"):
            if (a, b) in seen_edges:
                continue
            new = {a, b} - kept - want
            # Skip rather than stop: a later edge may still fit whatever budget is left, and the
            # deterministic order makes *which* ones reproducible across calls and re-derives.
            if len(kept) + len(want) + len(new) > max_nodes:
                continue
            want |= new
            seen_edges.add((a, b))
            edges.append({"source_id": a, "target_id": b})
        kept.update(want)
        for sid, name, kind in gs.conn.execute("SELECT sid, name, kind FROM symbols ORDER BY sid"):
            if sid in want:
                nodes.append({"id": sid, "name": name, "kind": kind})
            elif sid in kept or sid in fill:
                continue  # already emitted, or already queued, by an overlapping member
            elif len(kept) + len(fill) < max_nodes:
                # Unconnected symbols still export: 15 fleet stores hold no edges at all and
                # would otherwise return nothing. They only ever take budget left spare.
                fill[sid] = {"id": sid, "name": name, "kind": kind}
            elif len(nodes) == len(kept):
                break  # every wanted sid found and no spare budget — stop scanning this store

    federated_map(project, _export)
    # `kept` grows as later members are walked, so a sid queued as unconnected can since have
    # been claimed by an edge and already emitted above.
    spare = [n for sid, n in fill.items() if sid not in kept]
    nodes.extend(spare[: max_nodes - len(nodes)])
    return {"nodes": nodes, "edges": edges}


async def _api_graph_export(request: Request) -> JSONResponse:
    import asyncio
    project = request.query_params.get("project", "")
    max_nodes = int(request.query_params.get("max_nodes", "5000"))
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    return JSONResponse(await asyncio.to_thread(_graph_export_sync, project, max_nodes))


def register(app) -> None:
    app.add_route("/api/graph_export", _api_graph_export, methods=["GET"])
