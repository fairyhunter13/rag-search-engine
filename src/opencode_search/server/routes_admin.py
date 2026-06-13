"""Admin and client-tracking HTTP routes."""
from __future__ import annotations

import time

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from opencode_search.core.registry import list_projects

_clients: dict[str, dict] = {}
_start_time = time.monotonic()


async def _root(request: Request) -> RedirectResponse:
    return RedirectResponse("/dashboard")


async def _admin_status(request: Request) -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "uptime_s": round(time.monotonic() - _start_time),
        "clients": len(_clients),
        "projects": len(list_projects()),
    })


async def _admin_client_open(request: Request) -> JSONResponse:
    body = await request.json()
    client_id = body.get("client_id", "")
    cwd = body.get("cwd", "")
    if not client_id:
        return JSONResponse({"error": "client_id required"}, status_code=400)
    _clients[client_id] = {"cwd": cwd, "last_seen": time.monotonic()}
    return JSONResponse({"status": "ok", "client_id": client_id})


async def _admin_client_heartbeat(request: Request) -> JSONResponse:
    body = await request.json()
    client_id = body.get("client_id", "")
    if client_id in _clients:
        _clients[client_id]["last_seen"] = time.monotonic()
    return JSONResponse({"status": "ok"})


async def _admin_client_close(request: Request) -> JSONResponse:
    body = await request.json()
    client_id = body.get("client_id", "")
    _clients.pop(client_id, None)
    return JSONResponse({"status": "ok"})


def register(app) -> None:
    app.add_route("/", _root, methods=["GET"])
    app.add_route("/admin/status", _admin_status, methods=["GET"])
    app.add_route("/admin/client/open", _admin_client_open, methods=["POST"])
    app.add_route("/admin/client/heartbeat", _admin_client_heartbeat, methods=["POST"])
    app.add_route("/admin/client/close", _admin_client_close, methods=["POST"])
