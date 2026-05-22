"""Tool handler implementations for the MCP server.

Each handler validates arguments, drives the indexer/searcher, and returns
a JSON-serialisable dict that FastMCP transmits to the client.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from pathlib import Path
from typing import Any

from opencode_search.config import (
    FINAL_TOP_K,
    ProjectEntry,
    get_tier_dims,
    get_tier_models,
    load_registry,
    save_registry,
)
from opencode_search.indexer import index_project as _index_project, index_files as _index_files
from opencode_search.search import clear_search_cache, search
from opencode_search.storage import Storage
from opencode_search.watcher import watcher_manager

log = logging.getLogger(__name__)

_VALID_TIERS = {"premium", "balanced", "budget"}

# Track in-progress indexing runs so callers can poll status
_indexing_status: dict[str, dict[str, Any]] = {}
_indexing_lock = asyncio.Lock()


def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


# ---------------------------------------------------------------------------
# index_project
# ---------------------------------------------------------------------------


async def handle_index_project(
    path: str,
    tier: str = "balanced",
    watch: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Index a project directory and optionally start watching it."""
    if tier not in _VALID_TIERS:
        return {"error": f"Invalid tier '{tier}'. Choose: {sorted(_VALID_TIERS)}"}

    project_path = Path(path).expanduser().resolve()
    if not project_path.is_dir():
        return {"error": f"Directory not found: {project_path}"}

    path_str = str(project_path)

    async with _indexing_lock:
        if path_str in _indexing_status and _indexing_status[path_str].get("running"):
            return {"status": "already_indexing", "path": path_str}
        _indexing_status[path_str] = {"running": True, "started_at": _now_iso()}

    dims = get_tier_dims(tier)
    db_path = str(project_path / ".opencode" / f"index_{tier}")

    storage = Storage(db_path=db_path, dims=dims)
    await storage.open()
    try:
        t0 = time.perf_counter()
        result = await _index_project(storage, project_path, tier=tier, force=force)
        elapsed = time.perf_counter() - t0
    finally:
        await storage.close()

    # Update / insert registry entry
    registry = load_registry()
    entry = registry.get(path_str)
    if entry is None:
        entry = ProjectEntry(
            path=path_str,
            db_path=db_path,
            tier=tier,
            dims=dims,
            indexed_at=_now_iso(),
            file_count=result.files_indexed,
            watch=watch,
        )
    else:
        entry.tier = tier
        entry.db_path = db_path
        entry.dims = dims
        entry.indexed_at = _now_iso()
        entry.file_count = result.files_indexed
        entry.watch = watch
    registry[path_str] = entry
    save_registry(registry)

    clear_search_cache()

    # Start watcher if requested
    if watch and not watcher_manager.is_active(path_str):
        _dims = dims
        _db_path = db_path
        _tier = tier

        async def on_change(modified: list[Path], deleted: list[str]) -> None:
            st = Storage(db_path=_db_path, dims=_dims)
            await st.open()
            try:
                if deleted:
                    from opencode_search.cleaner import remove_chunks_for_paths
                    await remove_chunks_for_paths(st, deleted)
                if modified:
                    await _index_files(st, modified, tier=_tier)
                clear_search_cache()
            finally:
                await st.close()

        await watcher_manager.start(path_str, on_change=on_change)

    status: dict[str, Any] = {
        "status": "ok",
        "path": path_str,
        "tier": tier,
        "files_indexed": result.files_indexed,
        "files_unchanged": result.files_unchanged,
        "files_removed": result.files_removed,
        "chunks_total": result.chunks_total,
        "errors": result.errors,
        "elapsed_s": round(elapsed, 2),
        "watching": watcher_manager.is_active(path_str),
    }
    async with _indexing_lock:
        _indexing_status[path_str] = {"running": False, **status}
    return status


# ---------------------------------------------------------------------------
# search_code
# ---------------------------------------------------------------------------


async def handle_search_code(
    query: str,
    project_paths: list[str] | None = None,
    top_k: int = FINAL_TOP_K,
    use_rerank: bool = True,
) -> dict[str, Any]:
    """Search indexed projects for code matching the query."""
    if not query.strip():
        return {"error": "Query must not be empty"}

    registry = load_registry()
    if not registry:
        return {"results": [], "note": "No indexed projects. Run index_project first."}

    if project_paths:
        requested = {str(Path(p).expanduser().resolve()) for p in project_paths}
        projects = [v for k, v in registry.items() if k in requested]
        if not projects:
            return {"error": f"None of the requested paths are indexed: {project_paths}"}
    else:
        projects = list(registry.values())

    t0 = time.perf_counter()
    results = await search(query, projects=projects, top_k=top_k, use_rerank=use_rerank)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    return {
        "results": [
            {
                "path": r.path,
                "content": r.content,
                "language": r.language,
                "start_line": r.start_line,
                "end_line": r.end_line,
                "score": round(r.score, 4),
                "project_path": r.project_path,
            }
            for r in results
        ],
        "elapsed_ms": round(elapsed_ms, 1),
        "query": query,
        "projects_searched": len(projects),
    }


# ---------------------------------------------------------------------------
# project_status
# ---------------------------------------------------------------------------


async def handle_project_status(path: str) -> dict[str, Any]:
    """Return the current indexing and watching status of a project."""
    project_path = str(Path(path).expanduser().resolve())
    registry = load_registry()
    entry = registry.get(project_path)

    if entry is None:
        return {"indexed": False, "path": project_path}

    chunk_count: int | None = None
    try:
        storage = Storage(db_path=str(entry.db_path), dims=entry.dims)
        await storage.open()
        chunk_count = await storage.count()
        await storage.close()
    except Exception:
        pass

    in_progress = _indexing_status.get(project_path, {})
    watching = watcher_manager.is_active(project_path)

    return {
        "indexed": True,
        "path": project_path,
        "tier": entry.tier,
        "db_path": str(entry.db_path),
        "chunks": chunk_count,
        "watching": watching,
        "indexed_at": entry.indexed_at,
        "file_count": entry.file_count,
        "indexing_running": in_progress.get("running", False),
    }


# ---------------------------------------------------------------------------
# list_indexed_projects
# ---------------------------------------------------------------------------


async def handle_list_indexed_projects() -> dict[str, Any]:
    """Return all projects currently registered in the index registry."""
    registry = load_registry()
    active = set(watcher_manager.list_active())
    return {
        "projects": [
            {
                "path": p.path,
                "tier": p.tier,
                "db_path": str(p.db_path),
                "watching": p.path in active,
                "indexed_at": p.indexed_at,
                "file_count": p.file_count,
            }
            for p in registry.values()
        ]
    }


# ---------------------------------------------------------------------------
# stop_watching
# ---------------------------------------------------------------------------


async def handle_stop_watching(path: str) -> dict[str, Any]:
    """Stop the file-watcher for a project."""
    project_path = str(Path(path).expanduser().resolve())
    was_watching = watcher_manager.is_active(project_path)
    await watcher_manager.stop(project_path)

    registry = load_registry()
    if project_path in registry:
        registry[project_path].watch = False
        save_registry(registry)

    return {
        "path": project_path,
        "was_watching": was_watching,
        "status": "stopped",
    }
