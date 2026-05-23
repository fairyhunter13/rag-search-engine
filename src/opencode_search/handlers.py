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
    get_project_db_path,
    get_tier_dims,
    load_registry,
    save_registry,
)
from opencode_search.discover import is_indexable_file
from opencode_search.indexer import index_files as _index_files
from opencode_search.indexer import index_project as _index_project
from opencode_search.search import clear_search_cache, search
from opencode_search.storage import Storage
from opencode_search.watcher import watcher_manager

log = logging.getLogger(__name__)

_VALID_TIERS = {"premium", "balanced", "budget"}

# Track in-progress indexing runs so callers can poll status
_indexing_status: dict[str, dict[str, Any]] = {}
_indexing_lock = asyncio.Lock()


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def resolve_indexed_project_path(path: str) -> str | None:
    """Return the nearest indexed project root containing ``path``."""
    candidate = Path(path).expanduser().resolve()
    registry = load_registry()
    best_match: str | None = None
    best_depth = -1
    for project_path in registry:
        project_root = Path(project_path)
        try:
            candidate.relative_to(project_root)
        except ValueError:
            continue
        depth = len(project_root.parts)
        if depth > best_depth:
            best_match = project_path
            best_depth = depth
    return best_match


def _build_incremental_on_change(
    *,
    db_path: str,
    dims: int,
    tier: str,
    project_root: Path,
):
    async def on_change(modified: list[Path], deleted: list[str]) -> None:
        st = Storage(db_path=db_path, dims=dims)
        await st.open()
        try:
            if deleted:
                from opencode_search.cleaner import remove_chunks_for_paths

                project_deleted = [
                    p for p in deleted if ".opencode" not in Path(p).parts
                ]
                await remove_chunks_for_paths(st, project_deleted)
            if modified:
                project_modified = [
                    p for p in modified if is_indexable_file(p, root=project_root)
                ]
                await _index_files(st, project_modified, tier=tier)
            clear_search_cache()
        finally:
            await st.close()

    return on_change


# ---------------------------------------------------------------------------
# index_project
# ---------------------------------------------------------------------------


async def handle_index_project(
    path: str,
    tier: str = "balanced",
    watch: bool = False,
    force: bool = False,
    follow_symlinks: bool = True,
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

    status: dict[str, Any] | None = None
    try:
        dims = get_tier_dims(tier)
        db_path = get_project_db_path(project_path, tier)

        storage = Storage(db_path=db_path, dims=dims)
        await storage.open()
        try:
            t0 = time.perf_counter()
            result = await _index_project(storage, project_path, tier=tier, force=force, follow_symlinks=follow_symlinks)
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
                file_count=result.files_indexed + result.files_unchanged,
                watch=watch,
            )
        else:
            entry.tier = tier
            entry.db_path = db_path
            entry.dims = dims
            entry.indexed_at = _now_iso()
            entry.file_count = result.files_indexed + result.files_unchanged
            # A plain re-index should not implicitly disable an active watcher.
            # `stop-watching` is the explicit API for turning watch mode off.
            entry.watch = entry.watch or watch
        registry[path_str] = entry
        save_registry(registry)

        clear_search_cache()

        # Start watcher if requested
        if watch and not watcher_manager.is_active(path_str):
            await watcher_manager.start(
                path_str,
                on_change=_build_incremental_on_change(
                    db_path=db_path,
                    dims=dims,
                    tier=tier,
                    project_root=project_path,
                ),
            )

        status = {
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
        return status
    except Exception as exc:  # noqa: BLE001
        log.exception("index_project failed for %s", path_str)
        status = {"status": "error", "path": path_str, "error": str(exc)}
        return status
    finally:
        async with _indexing_lock:
            _indexing_status[path_str] = {"running": False, **(status or {})}


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
    try:
        results = await search(query, projects=projects, top_k=top_k, use_rerank=use_rerank)
    except ValueError as exc:
        return {"error": str(exc)}
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
        if Path(entry.db_path).exists():
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


async def handle_ensure_project_watching(path: str, *, persist: bool = False) -> dict[str, Any]:
    """Start watching the nearest indexed project containing ``path``."""
    project_path = resolve_indexed_project_path(path)
    if project_path is None:
        return {
            "status": "not_indexed",
            "indexed": False,
            "path": str(Path(path).expanduser().resolve()),
            "watching": False,
        }

    registry = load_registry()
    entry = registry[project_path]
    if persist and not entry.watch:
        entry.watch = True
        save_registry(registry)

    if watcher_manager.is_active(project_path):
        return {
            "status": "ok",
            "indexed": True,
            "path": project_path,
            "watching": True,
            "already_watching": True,
        }

    started = await watcher_manager.start(
        project_path,
        on_change=_build_incremental_on_change(
            db_path=str(entry.db_path),
            dims=entry.dims,
            tier=entry.tier,
            project_root=Path(project_path),
        ),
    )
    if not started:
        return {
            "status": "error",
            "indexed": True,
            "path": project_path,
            "watching": False,
            "error": "watcher start failed",
        }

    return {
        "status": "ok",
        "indexed": True,
        "path": project_path,
        "watching": True,
        "already_watching": False,
    }


async def handle_release_project_watch(path: str) -> dict[str, Any]:
    """Stop an auto-started watcher when the last attached client closes."""
    project_path = resolve_indexed_project_path(path)
    if project_path is None:
        return {
            "status": "not_indexed",
            "indexed": False,
            "path": str(Path(path).expanduser().resolve()),
            "watching": False,
        }

    registry = load_registry()
    entry = registry[project_path]
    if entry.watch:
        return {
            "status": "kept_persisted",
            "indexed": True,
            "path": project_path,
            "watching": watcher_manager.is_active(project_path),
        }

    was_watching = watcher_manager.is_active(project_path)
    if was_watching:
        await watcher_manager.stop(project_path)

    return {
        "status": "stopped" if was_watching else "not_watching",
        "indexed": True,
        "path": project_path,
        "watching": False,
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
