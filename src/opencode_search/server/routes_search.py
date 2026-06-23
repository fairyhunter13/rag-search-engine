"""Suggested-questions route."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse


async def _api_suggested_questions(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    from opencode_search.daemon.federation import federated_map
    rows = [r for _, rs in federated_map(project, lambda gs: gs.conn.execute(
        "SELECT title FROM communities WHERE level>=1 ORDER BY member_count DESC LIMIT 5"
    ).fetchall()) for r in rs]
    qs = list(dict.fromkeys(f"How does {r[0]} work?" for r in rows if r[0]))[:5]
    return JSONResponse({"questions": qs})


def register(app) -> None:
    app.add_route("/api/suggested_questions", _api_suggested_questions, methods=["GET"])
