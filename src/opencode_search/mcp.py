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

On startup the server GPU-checks the embedding layer and begins watching any
projects that were persisted with watch=True in the registry.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from opencode_search.handlers import (
    handle_index_project,
    handle_list_indexed_projects,
    handle_project_status,
    handle_search_code,
    handle_stop_watching,
)

log = logging.getLogger(__name__)

mcp = FastMCP(
    name="opencode-search",
    instructions=(
        "GPU-accelerated local semantic code search. "
        "Index your project with index_project, then call search_code. "
        "All embedding and reranking runs locally on your GPU — no data leaves your machine."
    ),
)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


@mcp.tool()
async def index_project(
    path: str,
    tier: str = "balanced",
    watch: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Index a project directory for semantic code search.

    GPU-accelerated embedding via ONNX Runtime (CUDAExecutionProvider).

    Args:
        path:  Absolute path to the project root directory.
        tier:  Embedding quality — "budget" (fast), "balanced" (default), "premium" (best).
        watch: Start a live file-watcher for incremental re-indexing.
        force: Re-index all files even if unchanged (ignores hash cache).
    """
    return await handle_index_project(path=path, tier=tier, watch=watch, force=force)


@mcp.tool()
async def search_code(
    query: str,
    project_paths: list[str] | None = None,
    top_k: int = 10,
    use_rerank: bool = True,
) -> dict[str, Any]:
    """Search indexed projects for code matching the query.

    Combines vector similarity (semantic) and BM25 full-text search,
    then reranks candidates with a cross-encoder on GPU.

    Args:
        query:         Natural language or code query string.
        project_paths: Limit search to these project paths. Searches all if omitted.
        top_k:         Maximum results to return (default 10).
        use_rerank:    Run cross-encoder reranking (slower but more accurate).
    """
    return await handle_search_code(
        query=query,
        project_paths=project_paths,
        top_k=top_k,
        use_rerank=use_rerank,
    )


@mcp.tool()
async def project_status(path: str) -> dict[str, Any]:
    """Get the current indexing and watching status for a project.

    Args:
        path: Absolute path to the project root directory.
    """
    return await handle_project_status(path=path)


@mcp.tool()
async def list_indexed_projects() -> dict[str, Any]:
    """List all projects that have been indexed."""
    return await handle_list_indexed_projects()


@mcp.tool()
async def stop_watching(path: str) -> dict[str, Any]:
    """Stop the live file-watcher for a project.

    Args:
        path: Absolute path to the project root directory.
    """
    return await handle_stop_watching(path=path)


# ---------------------------------------------------------------------------
# Startup: GPU guard + resume watchers from registry
# ---------------------------------------------------------------------------


async def _startup() -> None:
    """GPU guard + resume persisted watchers from registry."""
    from opencode_search.embeddings import assert_gpu_available
    assert_gpu_available()

    try:
        from opencode_search.config import get_tier_dims, get_tier_models, load_registry
        from opencode_search.cleaner import remove_chunks_for_paths
        from opencode_search.indexer import index_files
        from opencode_search.search import clear_search_cache
        from opencode_search.storage import Storage
        from opencode_search.watcher import watcher_manager

        registry = load_registry()
        for path_str, entry in registry.items():
            if not entry.watch:
                continue

            dims = get_tier_dims(entry.tier)
            db_path = entry.db_path
            tier = entry.tier

            async def make_cb(_db=db_path, _d=dims, _t=tier):
                async def on_change(modified: list[Path], deleted: list[str]) -> None:
                    st = Storage(db_path=_db, dims=_d)
                    await st.open()
                    try:
                        if deleted:
                            await remove_chunks_for_paths(st, deleted)
                        if modified:
                            await index_files(st, modified, tier=_t)
                        clear_search_cache()
                    finally:
                        await st.close()
                return on_change

            cb = await make_cb()
            ok = await watcher_manager.start(path_str, on_change=cb)
            if ok:
                log.info("Resumed watcher for %s", path_str)
    except Exception as exc:
        log.warning("Failed to resume watchers: %s", exc)


# ---------------------------------------------------------------------------
# Server entrypoint
# ---------------------------------------------------------------------------


def run_mcp_server() -> None:
    """Start the MCP server with stdio transport."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        asyncio.get_event_loop().run_until_complete(_startup())
    except Exception as exc:
        log.critical("Startup failed: %s", exc)
        sys.exit(1)

    mcp.run(transport="stdio")
