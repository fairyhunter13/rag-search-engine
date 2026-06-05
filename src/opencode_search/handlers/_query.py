"""Query handlers: search_code, project_status, list_indexed_projects."""
from __future__ import annotations

import contextlib
import logging
import time
from pathlib import Path
from typing import Any

from opencode_search.config import FINAL_TOP_K, load_registry
from opencode_search.metrics import record_search
from opencode_search.search import search
from opencode_search.storage import Storage
from opencode_search.watcher import watcher_manager

from ._common import _indexing_status, _touch_projects_last_active

log = logging.getLogger(__name__)


async def handle_search_code(
    query: str,
    project_paths: list[str] | None = None,
    top_k: int = FINAL_TOP_K,
    use_rerank: bool = True,
    content_types: list[str] | None = None,
    include_federation: bool = False,
) -> dict[str, Any]:
    """Search indexed projects for code matching the query."""
    if not query.strip():
        return {"error": "Query must not be empty"}

    registry = load_registry()
    if not registry:
        return {"results": [], "note": "No indexed projects. Run index_project first."}

    if project_paths:
        resolved = [str(Path(p).expanduser().resolve()) for p in project_paths]
        if include_federation:
            from opencode_search.handlers._federation import _expand_with_federation
            resolved = _expand_with_federation(resolved, registry)
        requested = set(resolved)
        projects = [v for k, v in registry.items() if k in requested]
        if not projects:
            return {"error": f"None of the requested paths are indexed: {project_paths}"}
    else:
        projects = list(registry.values())

    t0 = time.perf_counter()
    # Rerank is enforced for correctness; ignore caller requests to disable it.
    try:
        results = await search(query, projects=projects, top_k=top_k, use_rerank=True)
    except ValueError as exc:
        return {"error": str(exc)}
    elapsed_ms = (time.perf_counter() - t0) * 1000

    if content_types:
        ct_set = set(content_types)
        results = [r for r in results if r.language in ct_set]

    top_score = results[0].score if results else None
    record_search(elapsed_ms, len(results), top_score)
    with contextlib.suppress(Exception):
        _touch_projects_last_active(projects)

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


async def handle_project_status(path: str) -> dict[str, Any]:
    """Return the current indexing and watching status of a project."""
    project_path = str(Path(path).expanduser().resolve())
    registry = load_registry()
    entry = registry.get(project_path)

    if entry is None:
        in_progress = _indexing_status.get(project_path, {})
        return {
            "indexed": False,
            "path": project_path,
            "indexing_running": in_progress.get("running", False),
            "started_at": in_progress.get("started_at"),
        }

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
    # watcher_manager.is_active() is process-local; entry.watch is the persisted
    # intent. Show True if either.
    watching = watcher_manager.is_active(project_path) or entry.watch

    return {
        "indexed": True,
        "path": project_path,
        "db_path": str(entry.db_path),
        "chunks": chunk_count,
        "watching": watching,
        "indexed_at": entry.indexed_at,
        "file_count": entry.file_count,
        "indexing_running": in_progress.get("running", False),
    }


def _count_communities(project_path: str) -> int:
    """Return community count from graph.db, 0 if unavailable."""
    import sqlite3

    from opencode_search.config import get_project_graph_db_path
    db_path = get_project_graph_db_path(project_path)
    with contextlib.suppress(Exception):
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            (count,) = conn.execute("SELECT COUNT(*) FROM communities").fetchone()
            return int(count)
        finally:
            conn.close()
    return 0


async def handle_list_indexed_projects() -> dict[str, Any]:
    """Return all projects currently registered in the index registry."""
    import asyncio
    registry = load_registry()
    active = set(watcher_manager.list_active())
    rows = []
    for p in registry.values():
        community_count = await asyncio.to_thread(_count_communities, p.path)
        rows.append({
            "path": p.path,
            "db_path": str(p.db_path),
            "watching": p.path in active or p.watch,
            "indexed_at": p.indexed_at,
            "file_count": p.file_count,
            "communities": community_count,
        })
    return {"projects": rows}
