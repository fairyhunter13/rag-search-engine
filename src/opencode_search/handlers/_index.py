"""Index-project handler: drives the indexer, registry, and watcher setup."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Any

from opencode_search.config import (
    ProjectEntry,
    get_project_db_path,
    get_tier_dims,
    load_registry,
    save_registry,
)
from opencode_search.discover import is_indexable_file_with_config
from opencode_search.embeddings import get_embed_workers_gpu
from opencode_search.index_config import ProjectConfig, load_project_config
from opencode_search.indexer import index_files as _index_files
from opencode_search.indexer import index_project as _index_project
from opencode_search.search import clear_search_cache
from opencode_search.storage import Storage
from opencode_search.watcher import watcher_manager

from ._common import _VALID_TIERS, _indexing_lock, _indexing_status, _now_iso

log = logging.getLogger(__name__)


def _build_incremental_on_change(
    *,
    db_path: str,
    dims: int,
    tier: str,
    project_root: Path,
) -> Any:
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
                try:
                    project_cfg: ProjectConfig | None = load_project_config(project_root)
                except Exception:
                    project_cfg = None
                project_modified = [
                    p
                    for p in modified
                    if is_indexable_file_with_config(p, root=project_root, project_config=project_cfg)
                ]
                await _index_files(st, project_modified, tier=tier, project_root=project_root)
            clear_search_cache()
        finally:
            await st.close()

    return on_change


async def _run_index_project(
    path_str: str,
    project_path: Path,
    tier: str,
    watch: bool,
    force: bool,
    follow_symlinks: bool,
    on_complete: Any,
    on_progress: Any = None,
) -> None:
    """Background task that performs the actual indexing work."""
    status: dict[str, Any] | None = None
    try:
        dims = get_tier_dims(tier)
        db_path = get_project_db_path(project_path, tier)

        # Start watcher before indexing so no file changes are missed during
        # the index scan. For large projects (20K files) hashing alone takes
        # 30-60s — without early-start, changes during that window are lost.
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

        storage = Storage(db_path=db_path, dims=dims)
        await storage.open()
        try:
            # Compact fragmented txn log before indexing to avoid the memory
            # spike caused by LanceDB loading thousands of tiny transaction files.
            # Skip when force=True: the table is cleared immediately after open,
            # so compacting before clearing is wasted work (~200s on 109K chunks).
            if not force:
                await storage.compact_before_index()
            t0 = time.perf_counter()
            result = await _index_project(
                storage, project_path,
                tier=tier, force=force, follow_symlinks=follow_symlinks,
                embed_workers=min(2, get_embed_workers_gpu()),
                # 8 file workers: I/O-bound hashing/reading keeps GPU fed even
                # when 2-3 slots are blocked by large files (>1MB).
                file_workers=8,
                progress_callback=on_progress,
            )
            elapsed = time.perf_counter() - t0
        finally:
            await storage.close()

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
            entry.watch = entry.watch or watch
        entry.last_active = _now_iso()
        registry[path_str] = entry
        save_registry(registry)

        clear_search_cache()

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

        if on_complete is not None:
            with contextlib.suppress(Exception):
                await on_complete(status)
    except Exception as exc:
        log.exception("index_project failed for %s", path_str)
        status = {"status": "error", "path": path_str, "error": str(exc)}
    finally:
        async with _indexing_lock:
            _indexing_status[path_str] = {"running": False, **(status or {})}


async def handle_index_project(
    path: str,
    tier: str = "balanced",
    watch: bool = False,
    force: bool = False,
    follow_symlinks: bool = True,
    on_complete: Any = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Index a project directory and optionally start watching it.

    Returns immediately with ``status="indexing"``; the actual work runs in
    the background.  Poll ``project_status`` to check for completion.
    """
    if tier not in _VALID_TIERS:
        return {"error": f"Invalid tier '{tier}'. Choose: {sorted(_VALID_TIERS)}"}

    project_path = Path(path).expanduser().resolve()
    if not project_path.is_dir():
        return {"error": f"Directory not found: {project_path}"}

    path_str = str(project_path)

    async with _indexing_lock:
        if path_str in _indexing_status and _indexing_status[path_str].get("running"):
            return {"status": "already_indexing", "path": path_str}
        started_at = _now_iso()
        _indexing_status[path_str] = {"running": True, "started_at": started_at}

    _indexing_task = asyncio.create_task(
        _run_index_project(
            path_str=path_str,
            project_path=project_path,
            tier=tier,
            watch=watch,
            force=force,
            follow_symlinks=follow_symlinks,
            on_complete=on_complete,
            on_progress=on_progress,
        ),
        name=f"index-{path_str}",
    )
    _indexing_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    return {"status": "indexing", "path": path_str, "started_at": started_at}
