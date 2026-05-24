"""MCP server for opencode-search — stdio transport via FastMCP.

Exposes 5 tools:
  index_project          — index a directory (GPU-accelerated)
  search_code            — semantic + keyword hybrid search with reranking
  project_status         — get indexing state for a directory
  list_indexed_projects  — enumerate all registered projects
  stop_watching          — stop the file-watcher for a project

Usage:
  opencode-search mcp           # stdio (for AI assistants)
  python -m opencode_search mcp # equivalent

On startup the server GPU-checks the embedding layer. Persisted watchers are
resumed automatically on the first public tool call, bound to FastMCP's loop.

Event-loop strategy:
  FastMCP owns the event loop. The GPU guard runs synchronously (no loop
  required), and watcher resume is performed lazily from public tools so the
  watchers bind to FastMCP's loop — NOT a transient pre-startup loop that closes.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError as exc:  # pragma: no cover - exercised in dep-light envs
    from opencode_search._fastmcp_stub import FastMCPStub as FastMCP

    _MCP_IMPORT_ERROR = exc
else:
    _MCP_IMPORT_ERROR = None

from starlette.responses import JSONResponse

from opencode_search.daemon import DEFAULT_CLIENT_STALE_S, DEFAULT_MODEL_IDLE_UNLOAD_S, _global_prompt_text
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

_mcp_kwargs: dict[str, Any] = {
    "name": "opencode-search",
    "instructions": (
        "GPU-accelerated local semantic code search — all embedding and reranking runs locally"
        " on your GPU, no data leaves your machine.\n\n"
        + _global_prompt_text()
    ),
}
if _MCP_IMPORT_ERROR is not None:
    _mcp_kwargs["missing_exc"] = _MCP_IMPORT_ERROR

mcp = FastMCP(**_mcp_kwargs)

_stale_cleanup_task: asyncio.Task[None] | None = None
_stale_cleanup_lock: asyncio.Lock | None = None


async def _release_stale_project_watches() -> None:
    for project_path in runtime_state.releaseable_stale_projects(DEFAULT_CLIENT_STALE_S):
        await handle_release_project_watch(project_path)


async def _stale_cleanup_loop() -> None:
    interval_s = max(1.0, min(float(DEFAULT_CLIENT_STALE_S) / 3.0, 5.0))
    while True:
        try:
            await asyncio.sleep(interval_s)
            await _release_stale_project_watches()
            # Unload embedding/reranker models when idle to reclaim RAM/VRAM.
            # Models reload automatically on the next search (~2-5s warm-up).
            if DEFAULT_MODEL_IDLE_UNLOAD_S > 0:
                from opencode_search.embeddings import (
                    cleanup_models,
                    seconds_since_last_inference,
                )
                if seconds_since_last_inference() > DEFAULT_MODEL_IDLE_UNLOAD_S:
                    log.info(
                        "models idle >%ds — unloading to free RAM/VRAM",
                        DEFAULT_MODEL_IDLE_UNLOAD_S,
                    )
                    await asyncio.to_thread(cleanup_models)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("stale cleanup failed: %s", exc)


async def _ensure_stale_cleanup_started() -> None:
    global _stale_cleanup_lock, _stale_cleanup_task
    if _stale_cleanup_task is not None and not _stale_cleanup_task.done():
        return
    if _stale_cleanup_lock is None:
        _stale_cleanup_lock = asyncio.Lock()
    async with _stale_cleanup_lock:
        if _stale_cleanup_task is not None and not _stale_cleanup_task.done():
            return
        _stale_cleanup_task = asyncio.create_task(_stale_cleanup_loop())


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
    await _ensure_stale_cleanup_started()
    runtime_state.note_activity()
    await _release_stale_project_watches()
    await _ensure_watchers_resumed()
    result = await handle_index_project(path=path, tier=tier, watch=watch, force=force, follow_symlinks=follow_symlinks)
    project_path = str(result.get("path", "")) if isinstance(result, dict) else ""
    if result.get("status") == "ok" and project_path:
        bound_clients = runtime_state.bind_clients_to_project(project_path)
        if bound_clients > 0:
            await handle_ensure_project_watching(project_path, persist=False)
    return result


@mcp.tool()
async def search_code(
    query: str,
    project_paths: list[str] | None = None,
    top_k: int = 10,
    use_rerank: bool = True,
) -> dict[str, Any]:
    """Search indexed projects for code matching the query."""
    await _ensure_stale_cleanup_started()
    runtime_state.note_activity()
    await _release_stale_project_watches()
    await _ensure_watchers_resumed()
    return await handle_search_code(
        query=query,
        project_paths=project_paths,
        top_k=top_k,
        use_rerank=use_rerank,
    )


@mcp.tool()
async def project_status(path: str) -> dict[str, Any]:
    """Get the current indexing and watching status for a project."""
    await _ensure_stale_cleanup_started()
    runtime_state.note_activity()
    await _release_stale_project_watches()
    await _ensure_watchers_resumed()
    return await handle_project_status(path=path)


@mcp.tool()
async def list_indexed_projects() -> dict[str, Any]:
    """List all projects that have been indexed."""
    await _ensure_stale_cleanup_started()
    runtime_state.note_activity()
    await _release_stale_project_watches()
    await _ensure_watchers_resumed()
    return await handle_list_indexed_projects()


@mcp.tool()
async def stop_watching(path: str) -> dict[str, Any]:
    """Stop the live file-watcher for a project."""
    await _ensure_stale_cleanup_started()
    runtime_state.note_activity()
    await _release_stale_project_watches()
    await _ensure_watchers_resumed()
    return await handle_stop_watching(path=path)


@mcp.tool()
async def search_metrics() -> dict[str, Any]:
    """Return cumulative search_code call statistics for this daemon session.

    Tracks call count, zero-result rate, latency percentiles (p50/p95), and
    average top-score distribution — useful for measuring how effectively
    clients are using the search engine.
    """
    from opencode_search.metrics import get_metrics

    await _ensure_stale_cleanup_started()
    runtime_state.note_activity()
    return get_metrics()


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz(_request) -> JSONResponse:
    """Lightweight health endpoint for the singleton HTTP daemon."""
    await _ensure_stale_cleanup_started()
    await _release_stale_project_watches()
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
    await _ensure_stale_cleanup_started()
    await _release_stale_project_watches()
    project_path = resolve_indexed_project_path(cwd) if cwd else None
    runtime_state.client_open(client_id, cwd=cwd or None, project_path=project_path)
    await _ensure_watchers_resumed()
    if project_path:
        await handle_ensure_project_watching(project_path, persist=False)
    return JSONResponse({"ok": True, **runtime_state.snapshot()})


@mcp.custom_route("/admin/client/heartbeat", methods=["POST"], include_in_schema=False)
async def client_heartbeat(request) -> JSONResponse:
    payload = await request.json()
    await _ensure_stale_cleanup_started()
    await _release_stale_project_watches()
    runtime_state.client_heartbeat(str(payload.get("client_id", "")))
    return JSONResponse({"ok": True, **runtime_state.snapshot()})


@mcp.custom_route("/admin/client/close", methods=["POST"], include_in_schema=False)
async def client_close(request) -> JSONResponse:
    payload = await request.json()
    await _ensure_stale_cleanup_started()
    await _release_stale_project_watches()
    runtime_state.client_close(str(payload.get("client_id", "")))
    return JSONResponse({"ok": True, **runtime_state.snapshot()})


@mcp.custom_route("/admin/status", methods=["GET"], include_in_schema=False)
async def admin_status(_request) -> JSONResponse:
    await _ensure_stale_cleanup_started()
    await _release_stale_project_watches()
    return JSONResponse({"ok": True, **runtime_state.snapshot()})


# ---------------------------------------------------------------------------
# Async startup: resume persisted watchers (bound to FastMCP's event loop)
# ---------------------------------------------------------------------------


async def resume_watchers() -> None:
    """Restart any watchers that were persisted with watch=True in the registry.

    MUST be called from inside FastMCP's running event loop so that the watcher's
    `asyncio.run_coroutine_threadsafe(...)` dispatches to the right loop.
    """
    from opencode_search.config import load_registry

    registry = load_registry()
    for path_str, entry in registry.items():
        if not entry.watch:
            continue
        result = await handle_ensure_project_watching(path_str, persist=True)
        if result.get("watching"):
            log.info("Resumed watcher for %s", path_str)


# FastMCP doesn't expose a lifespan hook in this version, so public tools call
# this idempotent guard before doing work.
_resumed = False
_resume_lock: asyncio.Lock | None = None


async def _ensure_watchers_resumed() -> None:
    """Resume persisted watchers once, bound to FastMCP's active event loop."""
    global _resume_lock, _resumed
    if _resumed:
        return
    if _resume_lock is None:
        _resume_lock = asyncio.Lock()
    async with _resume_lock:
        if _resumed:
            return
        await resume_watchers()
        _resumed = True


async def _resume_persisted_watchers() -> dict[str, Any]:
    """Internal/testing helper: resume watchers from the registry."""
    try:
        await _ensure_watchers_resumed()
        return {"status": "ok"}
    except Exception as exc:
        log.warning("resume_watchers failed: %s", exc)
        return {"status": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# Synchronous GPU guard (run before the FastMCP loop starts)
# ---------------------------------------------------------------------------


def _gpu_guard() -> None:
    """Block startup if no GPU is available. CPUExecutionProvider is forbidden."""
    from opencode_search.embeddings import assert_gpu_available
    assert_gpu_available()


# ---------------------------------------------------------------------------
# Server entrypoint
# ---------------------------------------------------------------------------


def run_mcp_server() -> None:
    """Start the MCP server with stdio transport.

    The GPU guard runs synchronously (raises if no CUDA), then FastMCP takes
    ownership of the event loop. Watcher resume runs lazily on first tool call.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        _gpu_guard()
    except Exception as exc:
        log.critical("GPU guard failed: %s", exc)
        sys.exit(1)

    log.info("Starting opencode-search MCP server on stdio…")
    mcp.run(transport="stdio")


def run_mcp_http_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Start the MCP server over streamable HTTP for shared daemon usage."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        _gpu_guard()
    except Exception as exc:
        log.critical("GPU guard failed: %s", exc)
        sys.exit(1)

    mcp.settings.host = host
    mcp.settings.port = port
    log.info("Starting opencode-search MCP server on http://%s:%s/mcp", host, port)
    mcp.run(transport="streamable-http")
