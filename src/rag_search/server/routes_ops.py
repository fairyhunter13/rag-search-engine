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
    from rag_search.daemon.cpu_budget import (
        cpu_percent_core,
        cpu_quota_cores,
        cpu_throttle_stat,
        cpu_usage_nsec,
    )
    from rag_search.daemon.sweeps import _bpre_state
    from rag_search.graph.llm import llm_token_stats
    from rag_search.kb.llm_escalation import llm_cache_stats
    from rag_search.query.search import rerank_stats
    return {**_metrics, "rerank": rerank_stats(), "bpre": dict(_bpre_state),
            "llm_cache": llm_cache_stats(), "llm_tokens": llm_token_stats(),
            "cpu": {"percent_core": round(cpu_percent_core(), 4),
                    "quota_cores": cpu_quota_cores(), "usage_nsec": cpu_usage_nsec(),
                    **cpu_throttle_stat()}}


async def _api_metrics(request: Request) -> JSONResponse:
    return JSONResponse(_snapshot())


def _reload_exit_code(restart: bool) -> int:
    """Non-zero -> systemd Restart=on-failure restarts (reload); 0 -> stays down (stop)."""
    from rag_search.daemon import server
    server._REQUESTED_EXIT_CODE = 3 if restart else 0
    return server._REQUESTED_EXIT_CODE


def _parse_restart_param(value: str | None) -> bool:
    """?restart= query param, default true; only the literal 'false' (any case) means stop."""
    return (value or "true").lower() != "false"


async def _api_reload(request: Request) -> JSONResponse:
    import signal
    restart = _parse_restart_param(request.query_params.get("restart"))
    _reload_exit_code(restart)
    os.kill(os.getpid(), signal.SIGTERM)
    return JSONResponse({"status": "reloading" if restart else "stopping"})


async def _api_sweeps_pause(request: Request) -> JSONResponse:
    from rag_search.daemon import sweeps
    sweeps._PAUSED = True
    return JSONResponse({"status": "paused"})


async def _api_sweeps_resume(request: Request) -> JSONResponse:
    from rag_search.daemon import sweeps
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
