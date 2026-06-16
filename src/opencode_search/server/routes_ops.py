"""Metrics, health, reload, sweeps, and event-stream routes."""
from __future__ import annotations

import asyncio
import json
import os

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

_metrics: dict = {
    "search": {"count": 0},
    "chat_stream": {"stream_error_count": 0, "error_by_intent": {}},
}


def _snapshot() -> dict:
    from opencode_search.query.search import rerank_stats
    return {**_metrics, "rerank": rerank_stats()}


async def _api_metrics(request: Request) -> JSONResponse:
    return JSONResponse(_snapshot())


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


_event_subscribers: list[asyncio.Queue] = []


def publish_event(evt: dict) -> None:
    """Publish a job event to all active SSE subscribers (best-effort)."""
    import contextlib
    for q in list(_event_subscribers):
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait(evt)


async def _api_events_stream(request: Request) -> Response:
    q: asyncio.Queue = asyncio.Queue(maxsize=32)
    _event_subscribers.append(q)

    async def _gen():
        try:
            yield b'data: {"type":"connected"}\n\n'
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=10.0)
                    yield f"data: {json.dumps(evt)}\n\n".encode()
                except TimeoutError:
                    yield b'data: {"type":"keepalive"}\n\n'
        finally:
            _event_subscribers.remove(q)

    return StreamingResponse(_gen(), media_type="text/event-stream")


def register(app) -> None:
    app.add_route("/api/metrics", _api_metrics, methods=["GET"])
    app.add_route("/api/reload", _api_reload, methods=["POST"])
    app.add_route("/api/sweeps/pause", _api_sweeps_pause, methods=["POST"])
    app.add_route("/api/sweeps/resume", _api_sweeps_resume, methods=["POST"])
    app.add_route("/api/events/stream", _api_events_stream, methods=["GET"])
