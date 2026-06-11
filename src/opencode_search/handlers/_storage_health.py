"""_storage_health.py — Read-only storage diagnostics: stale index dirs, WAL size, recoverable space."""
from __future__ import annotations

import os
from typing import Any


def _count_index_dirs(indices_path: str) -> int:
    """Count UUID dirs inside chunks.lance/_indices/."""
    try:
        return sum(1 for e in os.scandir(indices_path) if e.is_dir())
    except OSError:
        return 0


def _dir_size_bytes(path: str) -> int:
    total = 0
    try:
        for root, _, files in os.walk(path):
            for f in files:
                with __import__("contextlib").suppress(OSError):
                    total += os.path.getsize(os.path.join(root, f))
    except OSError:
        pass
    return total


def _active_index_count(db_path: str, dims: int) -> int:
    """Return the number of ANN indexes reported by LanceDB for the chunks table."""
    try:
        import lancedb  # type: ignore[import]
        db = lancedb.connect(db_path)
        tbl = db.open_table("chunks")
        stats = tbl.stats()
        return getattr(stats, "num_indices", 0) or 0
    except Exception:
        return 0


def _project_storage_stats(project_path: str, db_path: str, dims: int) -> dict[str, Any]:
    from pathlib import Path

    index_root = Path(db_path)
    chunks_lance = index_root / "chunks.lance"
    data_dir = chunks_lance / "data"
    indices_dir = chunks_lance / "_indices"

    # Graph DB + WAL
    graph_db_path = index_root.parent / "graph.db"
    wal_path = index_root.parent / "graph.db-wal"
    graph_bytes = _dir_size_bytes(str(graph_db_path)) if graph_db_path.is_file() else 0
    wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0

    data_bytes = _dir_size_bytes(str(data_dir)) if data_dir.exists() else 0
    indices_bytes = _dir_size_bytes(str(indices_dir)) if indices_dir.exists() else 0
    total_bytes = _dir_size_bytes(str(index_root))

    on_disk_index_dirs = _count_index_dirs(str(indices_dir)) if indices_dir.exists() else 0
    active_index_count = _active_index_count(str(index_root), dims)
    stale_index_dirs = max(0, on_disk_index_dirs - active_index_count)

    # Estimate recoverable MB: stale index dir bytes + WAL over 4 MB
    indices_per_dir = indices_bytes / max(1, on_disk_index_dirs)
    stale_bytes = int(stale_index_dirs * indices_per_dir)
    recoverable_bytes = stale_bytes + max(0, wal_bytes - 4 * 1024 * 1024)
    recoverable_mb = round(recoverable_bytes / 1024 / 1024, 1)

    return {
        "project_path": project_path,
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / 1024 / 1024, 1),
        "data_bytes": data_bytes,
        "indices_bytes": indices_bytes,
        "graph_db_bytes": graph_bytes,
        "wal_bytes": wal_bytes,
        "wal_mb": round(wal_bytes / 1024 / 1024, 1),
        "active_index_count": active_index_count,
        "on_disk_index_dirs": on_disk_index_dirs,
        "stale_index_dirs": stale_index_dirs,
        "recoverable_mb": recoverable_mb,
    }


async def handle_storage_health(
    project_path: str | None = None,
    *,
    include_federation: bool = True,
) -> dict[str, Any]:
    """Return storage diagnostics: stale LanceDB index dirs, WAL size, recoverable space.

    If project_path is None, reports on all indexed projects.
    Fields per project: total_bytes, data_bytes, indices_bytes, wal_bytes,
    active_index_count, on_disk_index_dirs, stale_index_dirs, recoverable_mb.
    """
    import asyncio

    from opencode_search.config import get_project_index_dir, load_registry

    def _run() -> dict[str, Any]:
        registry = load_registry()
        if project_path:
            entries = {project_path: registry.get(project_path)} if project_path in registry else {}
        else:
            entries = dict(registry)

        projects_stats: list[dict[str, Any]] = []
        total_recoverable_mb = 0.0

        for path, entry in entries.items():
            if entry is None:
                continue
            try:
                idx_dir = get_project_index_dir(path) / "index"
                dims = int(getattr(entry, "dims", 768) or 768)
                stats = _project_storage_stats(path, str(idx_dir), dims)
                projects_stats.append(stats)
                total_recoverable_mb += stats["recoverable_mb"]
            except Exception as exc:
                projects_stats.append({"project_path": path, "error": str(exc)})

        return {
            "status": "ok",
            "project_count": len(projects_stats),
            "total_recoverable_mb": round(total_recoverable_mb, 1),
            "projects": projects_stats,
        }

    return await asyncio.to_thread(_run)
