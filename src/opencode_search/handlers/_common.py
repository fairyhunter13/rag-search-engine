"""Shared state and utilities used across handler submodules."""
from __future__ import annotations

import asyncio
import datetime
import logging
from pathlib import Path
from typing import Any

from opencode_search.config import (
    ProjectEntry,
    load_registry,
    save_registry,
)

log = logging.getLogger(__name__)

_LAST_ACTIVE_UPDATE_INTERVAL_S: int = 3600  # throttle registry writes to once per hour
_VALID_TIERS = {"premium", "balanced", "budget"}

# Shared across _index.py (writer) and _query.py (reader)
_indexing_status: dict[str, dict[str, Any]] = {}
_indexing_lock = asyncio.Lock()


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _touch_projects_last_active(projects: list[ProjectEntry]) -> None:
    """Update last_active for searched projects; throttled to once per hour."""
    now_dt = datetime.datetime.now(datetime.UTC)
    now_iso = now_dt.isoformat()
    threshold = datetime.timedelta(seconds=_LAST_ACTIVE_UPDATE_INTERVAL_S)
    registry = load_registry()
    changed = False
    for p in projects:
        entry = registry.get(p.path)
        if entry is None:
            continue
        stale = True
        if entry.last_active is not None:
            try:
                age = now_dt - datetime.datetime.fromisoformat(entry.last_active)
                stale = age > threshold
            except Exception:
                pass
        if stale:
            entry.last_active = now_iso
            changed = True
    if changed:
        save_registry(registry)


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
