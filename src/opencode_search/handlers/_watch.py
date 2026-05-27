"""Watch-project handlers: start, release, and stop file watchers."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from opencode_search.config import load_registry, save_registry
from opencode_search.watcher import watcher_manager

from ._common import resolve_indexed_project_path
from ._index import _build_incremental_on_change

log = logging.getLogger(__name__)


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
