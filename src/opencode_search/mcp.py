"""MCP server for opencode-search — stdio and streamable-HTTP transports via FastMCP.

Exposes 6 tools:
  index_project          — index a directory (GPU-accelerated)
  search_code            — semantic + keyword hybrid search with reranking
  project_status         — get indexing state for a directory
  list_indexed_projects  — enumerate all registered projects
  stop_watching          — stop the file-watcher for a project
  search_metrics         — return cumulative search statistics

Usage:
  opencode-search mcp           # stdio (for AI assistants)
  python -m opencode_search mcp # equivalent

Startup sequence (HTTP daemon):
  1. GPU guard runs synchronously before the event loop starts — exits with code 1
     if no CUDA provider is available (CPU fallback is forbidden).
  2. run_mcp_http_server builds the Starlette app, injects a combined lifespan that
     wraps session_manager.run() with our background tasks (stale-cleanup, watcher
     resumption) and fires sd_notify READY=1 after the session manager has started.
     On shutdown it cancels the cleanup task and fires STOPPING=1.

Note: FastMCP's lifespan= constructor param wires into _mcp_server, which for the
streamable-HTTP transport fires once per MCP session (not once per process), so it
cannot be used for process-level background tasks. The Starlette-level lifespan
injected in run_mcp_http_server is used instead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager, suppress
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError as exc:  # pragma: no cover
    from opencode_search._fastmcp_stub import FastMCPStub as FastMCP

    _MCP_IMPORT_ERROR = exc
else:
    _MCP_IMPORT_ERROR = None

from starlette.responses import JSONResponse

from opencode_search.daemon import (
    DEFAULT_CLIENT_STALE_S,
    DEFAULT_MODEL_IDLE_UNLOAD_S,
    _global_prompt_text,
    _sd_notify,
)
from opencode_search.daemon_runtime import runtime_state
from opencode_search.handlers import (
    handle_ensure_project_watching,
    handle_index_project,
    handle_list_indexed_projects,
    handle_project_status,
    handle_release_project_watch,
    handle_search_code,
    handle_stop_watching,
    resolve_indexed_project_path,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Background maintenance
# ---------------------------------------------------------------------------


async def _release_stale_project_watches() -> None:
    for project_path in runtime_state.releaseable_stale_projects(DEFAULT_CLIENT_STALE_S):
        await handle_release_project_watch(project_path)


async def _stale_cleanup_loop() -> None:
    interval_s = max(1.0, min(float(DEFAULT_CLIENT_STALE_S) / 3.0, 5.0))
    _watchdog_usec = int(os.environ.get("WATCHDOG_USEC", "0"))
    _watchdog_every = max(1, int(_watchdog_usec / 2_000_000)) if _watchdog_usec else 0
    _tick = 0
    while True:
        try:
            await asyncio.sleep(interval_s)
            _tick += 1
            await _release_stale_project_watches()
            if _watchdog_every and _tick % _watchdog_every == 0:
                _sd_notify("WATCHDOG=1\n")
            if DEFAULT_MODEL_IDLE_UNLOAD_S > 0:
                from opencode_search.embeddings import (
                    cleanup_models,
                    seconds_since_last_inference,
                )
                if seconds_since_last_inference() > DEFAULT_MODEL_IDLE_UNLOAD_S:
                    log.info("models idle >%ds — unloading to free RAM/VRAM", DEFAULT_MODEL_IDLE_UNLOAD_S)
                    await asyncio.to_thread(cleanup_models)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("stale cleanup failed: %s", exc)


async def resume_watchers() -> None:
    """Restart any watchers that were persisted with watch=True in the registry.

    Must be called from inside the running event loop so watcher coroutines
    bind to the correct loop.
    """
    from opencode_search.config import load_registry

    registry = load_registry()
    for path_str, entry in registry.items():
        if not entry.watch:
            continue
        result = await handle_ensure_project_watching(path_str, persist=True)
        if result.get("watching"):
            log.info("Resumed watcher for %s", path_str)


# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------

_mcp_kwargs: dict[str, Any] = {
    "name": "opencode-search",
    "instructions": (
        "GPU-accelerated local semantic code search — all embedding and reranking runs locally"
        " on your GPU, no data leaves your machine.\n\n"
        + _global_prompt_text()
    ),
    # No lifespan= here: the HTTP process-level lifespan is injected in
    # run_mcp_http_server (Starlette layer). FastMCP's lifespan= wires into
    # _mcp_server which fires per-session for HTTP — unsuitable for background tasks.
}
if _MCP_IMPORT_ERROR is not None:
    _mcp_kwargs["missing_exc"] = _MCP_IMPORT_ERROR

mcp = FastMCP(**_mcp_kwargs)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


@mcp.tool()
async def index_project(
    path: str,
    tier: str = "balanced",
    watch: bool = False,
    force: bool = False,
    follow_symlinks: bool = True,
) -> dict[str, Any]:
    """Index a project directory for semantic code search.

    GPU-accelerated embedding via ONNX Runtime (CUDAExecutionProvider).

    Args:
        path:            Absolute path to the project root directory.
        tier:            Embedding quality — "budget" (fast), "balanced" (default), "premium" (best).
        watch:           Start a live file-watcher for incremental re-indexing.
        force:           Re-index all files even if unchanged (ignores hash cache).
        follow_symlinks: Follow symlinked directories during indexing (default True; required for
                         monorepos that use symlinks to share code across services).
    """
    runtime_state.note_activity()

    async def _post_index(result: dict) -> None:
        pp = str(result.get("path", ""))
        if result.get("status") == "ok" and pp:
            bound_clients = runtime_state.bind_clients_to_project(pp)
            if bound_clients > 0:
                await handle_ensure_project_watching(pp, persist=False)

    return await handle_index_project(
        path=path, tier=tier, watch=watch, force=force,
        follow_symlinks=follow_symlinks, on_complete=_post_index,
    )


@mcp.tool()
async def search_code(
    query: str,
    project_paths: list[str] | None = None,
    top_k: int = 10,
    use_rerank: bool = True,
) -> dict[str, Any]:
    """Search indexed projects for code matching the query."""
    runtime_state.note_activity()
    return await handle_search_code(
        query=query,
        project_paths=project_paths,
        top_k=top_k,
        use_rerank=use_rerank,
    )


@mcp.tool()
async def project_status(path: str) -> dict[str, Any]:
    """Get the current indexing and watching status for a project."""
    runtime_state.note_activity()
    return await handle_project_status(path=path)


@mcp.tool()
async def list_indexed_projects() -> dict[str, Any]:
    """List all projects that have been indexed."""
    runtime_state.note_activity()
    return await handle_list_indexed_projects()


@mcp.tool()
async def stop_watching(path: str) -> dict[str, Any]:
    """Stop the live file-watcher for a project."""
    runtime_state.note_activity()
    return await handle_stop_watching(path=path)


@mcp.tool()
async def search_metrics() -> dict[str, Any]:
    """Return cumulative search_code call statistics for this daemon session.

    Tracks call count, zero-result rate, latency percentiles (p50/p95), and
    average top-score distribution — useful for measuring how effectively
    clients are using the search engine.
    """
    from opencode_search.metrics import get_metrics

    runtime_state.note_activity()
    return get_metrics()


# ---------------------------------------------------------------------------
# HTTP admin and health routes
# ---------------------------------------------------------------------------


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz(_request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "service": "opencode-search",
            "transport": "streamable-http",
            **runtime_state.snapshot(),
        }
    )


@mcp.custom_route("/admin/client/open", methods=["POST"], include_in_schema=False)
async def client_open(request) -> JSONResponse:
    payload = await request.json()
    client_id = str(payload.get("client_id", ""))
    cwd = str(payload.get("cwd", ""))
    project_path = resolve_indexed_project_path(cwd) if cwd else None
    runtime_state.client_open(client_id, cwd=cwd or None, project_path=project_path)
    if project_path:
        await handle_ensure_project_watching(project_path, persist=False)
    return JSONResponse({"ok": True, **runtime_state.snapshot()})


@mcp.custom_route("/admin/client/heartbeat", methods=["POST"], include_in_schema=False)
async def client_heartbeat(request) -> JSONResponse:
    payload = await request.json()
    runtime_state.client_heartbeat(str(payload.get("client_id", "")))
    return JSONResponse({"ok": True, **runtime_state.snapshot()})


@mcp.custom_route("/admin/client/close", methods=["POST"], include_in_schema=False)
async def client_close(request) -> JSONResponse:
    payload = await request.json()
    runtime_state.client_close(str(payload.get("client_id", "")))
    return JSONResponse({"ok": True, **runtime_state.snapshot()})


@mcp.custom_route("/admin/status", methods=["GET"], include_in_schema=False)
async def admin_status(_request) -> JSONResponse:
    return JSONResponse({"ok": True, **runtime_state.snapshot()})


# ---------------------------------------------------------------------------
# Server entrypoints
# ---------------------------------------------------------------------------


def run_mcp_server() -> None:
    """Start the MCP server with stdio transport (for AI assistant subprocesses)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        from opencode_search.embeddings import assert_gpu_available
        assert_gpu_available()
    except Exception as exc:
        log.critical("GPU guard failed: %s", exc)
        sys.exit(1)

    log.info("Starting opencode-search MCP server on stdio…")
    mcp.run(transport="stdio")


def run_mcp_http_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Start the MCP server over streamable HTTP for shared daemon usage."""
    import anyio
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        from opencode_search.embeddings import assert_gpu_available
        assert_gpu_available()
    except Exception as exc:
        log.critical("GPU guard failed: %s", exc)
        _sd_notify("STATUS=GPU guard failed — CUDA not available, refusing to start\n")
        sys.exit(1)

    starlette_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def _lifespan(app: Any) -> Any:
        cleanup_task = asyncio.create_task(_stale_cleanup_loop(), name="opencode-stale-cleanup")
        resume_task = asyncio.create_task(resume_watchers(), name="opencode-resume-watchers")
        resume_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        async with mcp.session_manager.run():
            _sd_notify("READY=1\n")
            _sd_notify(f"STATUS=listening on http://{host}:{port}/mcp\n")
            yield
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        _sd_notify("STOPPING=1\n")

    starlette_app.router.lifespan_context = _lifespan

    log.info("Starting opencode-search MCP server on http://%s:%s/mcp", host, port)

    async def _serve() -> None:
        config = uvicorn.Config(starlette_app, host=host, port=port, log_level="info")
        await uvicorn.Server(config).serve()

    anyio.run(_serve)
