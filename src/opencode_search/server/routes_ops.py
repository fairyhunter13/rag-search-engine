"""Metrics, health, reload, sweeps, and event-stream routes."""
from __future__ import annotations

import asyncio
import os
import time

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

_start_time = time.monotonic()
_alerts: list[dict] = []
_metrics: dict = {
    "search": {"count": 0},
    "chat_stream": {"stream_error_count": 0, "error_by_intent": {}},
}


async def _api_metrics(request: Request) -> JSONResponse:
    return JSONResponse(_metrics)


async def _api_metrics_history(request: Request) -> JSONResponse:
    return JSONResponse({"history": [], "hours": request.query_params.get("hours", "1")})


async def _api_system_status(request: Request) -> JSONResponse:
    from opencode_search.core.gpu import gpu_temp_c, vram_free_mb
    try:
        temp, vram = gpu_temp_c(), vram_free_mb()
    except Exception:
        temp, vram = 0, 0
    return JSONResponse({"uptime_s": round(time.monotonic() - _start_time),
                         "gpu_temp_c": temp, "vram_free_mb": vram, "ok": True})


async def _api_integrations_status(request: Request) -> JSONResponse:
    claude_md = os.path.expanduser("~/CLAUDE.md")
    has_block = False
    if os.path.exists(claude_md):
        with open(claude_md) as f:
            has_block = "[opencode-search-global-instructions" in f.read()
    return JSONResponse({"claude_md": has_block, "codex": False, "hermes": False})


async def _api_alerts_get(request: Request) -> JSONResponse:
    return JSONResponse({"alerts": _alerts})


async def _api_alerts_post(request: Request) -> JSONResponse:
    body = await request.json()
    _alerts.append({"level": body.get("level", "info"),
                    "message": body.get("message", ""), "ts": time.time()})
    return JSONResponse({"status": "ok"})


async def _api_reload(request: Request) -> JSONResponse:
    import signal
    os.kill(os.getpid(), signal.SIGTERM)
    return JSONResponse({"status": "reloading"})


async def _api_sweeps_pause(request: Request) -> JSONResponse:
    from opencode_search.daemon import sweeps
    sweeps._PAUSED = True
    return JSONResponse({"status": "paused"})


async def _api_sweeps_resume(request: Request) -> JSONResponse:
    from opencode_search.daemon import sweeps
    sweeps._PAUSED = False
    return JSONResponse({"status": "resumed"})


async def _api_git_hooks_get(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    hook = os.path.join(project, ".git", "hooks", "post-commit")
    return JSONResponse({"installed": os.path.exists(hook), "project": project})


async def _api_git_hooks_post(request: Request) -> JSONResponse:
    body = await request.json()
    project = body.get("project_path", "")
    action = body.get("action", "install")
    if not project:
        return JSONResponse({"error": "project_path required"}, status_code=400)
    hook = os.path.join(project, ".git", "hooks", "post-commit")
    if action == "install":
        os.makedirs(os.path.dirname(hook), exist_ok=True)
        with open(hook, "w") as f:
            f.write("#!/bin/sh\nopencode-search index .\n")
        os.chmod(hook, 0o755)
        return JSONResponse({"status": "installed"})
    if action == "uninstall" and os.path.exists(hook):
        os.remove(hook)
    return JSONResponse({"status": "uninstalled"})


async def _api_events_stream(request: Request) -> Response:
    async def _gen():
        yield b'data: {"type":"connected"}\n\n'
        for _ in range(6):
            await asyncio.sleep(10)
            yield b'data: {"type":"keepalive"}\n\n'
    return StreamingResponse(_gen(), media_type="text/event-stream")


def register(app) -> None:
    app.add_route("/api/metrics", _api_metrics, methods=["GET"])
    app.add_route("/api/metrics/history", _api_metrics_history, methods=["GET"])
    app.add_route("/api/system_status", _api_system_status, methods=["GET"])
    app.add_route("/api/integrations_status", _api_integrations_status, methods=["GET"])
    app.add_route("/api/alerts", _api_alerts_get, methods=["GET"])
    app.add_route("/api/alerts", _api_alerts_post, methods=["POST"])
    app.add_route("/api/reload", _api_reload, methods=["POST"])
    app.add_route("/api/sweeps/pause", _api_sweeps_pause, methods=["POST"])
    app.add_route("/api/sweeps/resume", _api_sweeps_resume, methods=["POST"])
    app.add_route("/api/git_hooks", _api_git_hooks_get, methods=["GET"])
    app.add_route("/api/git_hooks", _api_git_hooks_post, methods=["POST"])
    app.add_route("/api/events/stream", _api_events_stream, methods=["GET"])
