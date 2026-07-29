"""Metrics, health, reload, and sweeps routes."""
from __future__ import annotations

import os

from starlette.requests import Request
from starlette.responses import JSONResponse

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
    from rag_search.query.search import rerank_stats
    return {**_metrics, "rerank": rerank_stats(),
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


async def _api_gpu_release(request: Request) -> JSONResponse:
    """Hand the GPU-resident models' VRAM back now, instead of after 300 s of idle.

    The counterpart to /api/sweeps/pause: that one stops the daemon competing for the GPU
    in future, this one returns what it is already holding. Both exist for the same caller —
    a live test session sharing one card with the daemon — and pausing alone is not enough,
    because the BFC arena's high-water mark survives having nothing left to do.

    Safe under load: `release_models` clears the module singletons only, so a query already
    holding one keeps its session alive until it returns, and the next caller lazily rebuilds
    on a GPU EP (CPU fallback stays forbidden — see `Embedder._init`).
    """
    from rag_search.core.gpu import vram_free_mb
    from rag_search.daemon import server

    before = vram_free_mb()
    server.release_models()
    after = vram_free_mb()
    return JSONResponse({
        "status": "released",
        "vram_free_mb_before": round(before, 1),
        "vram_free_mb_after": round(after, 1),
    })


# The /api/events/stream job bus left with tier 3. Its only producer was the pipeline job runner
# (build_wiki / docgen / okf), so after R0 `publish_event` had no callers and the stream could only
# ever emit its own "connected" and "keepalive" frames. The dashboard's job chips, which were its
# only consumer, went with it. Indexing progress is reported by `overview(what="status")`, not by a
# pushed event — nothing else in the daemon ever published here.


def register(app) -> None:
    app.add_route("/api/metrics", _api_metrics, methods=["GET"])
    app.add_route("/api/reload", _api_reload, methods=["POST"])
    app.add_route("/api/sweeps/pause", _api_sweeps_pause, methods=["POST"])
    app.add_route("/api/sweeps/resume", _api_sweeps_resume, methods=["POST"])
    app.add_route("/api/gpu/release", _api_gpu_release, methods=["POST"])
