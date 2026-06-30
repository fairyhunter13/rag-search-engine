"""Federation: discover symlinked sub-repos and register them."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from opencode_search.core.config import IGNORED_DIRS

log = logging.getLogger(__name__)


def _looks_like_repo(target: Path) -> bool:
    from opencode_search.index.discover import iter_files
    return next(iter_files(target), None) is not None


def discover_members(root_path: str) -> list[str]:
    """Return resolved paths of nested symlinked dirs (any depth) that look like repos."""
    from opencode_search.core.config import federation_exclude_paths
    root = Path(root_path).resolve()
    exclude = federation_exclude_paths()
    members: list[str] = []
    try:
        for dirpath, dirs, _ in os.walk(str(root), followlinks=False):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
            for d in list(dirs):
                p = Path(dirpath) / d
                if not p.is_symlink():
                    continue
                target = p.resolve()
                if target == root or target.is_relative_to(root):
                    continue
                if str(target) in exclude:
                    dirs.remove(d)
                    continue
                if _looks_like_repo(target):
                    members.append(str(target))
                dirs.remove(d)
    except OSError:
        pass
    return members


def index_members(root_path: str) -> int:
    """Register all discovered federation members; persist root.federation. Returns count newly registered."""
    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.registry import get_project, upsert_project

    members = discover_members(root_path)
    registered = 0
    for m in members:
        if get_project(m) is None:
            upsert_project(ProjectEntry(path=m, enabled=True))
            registered += 1
    root_entry = get_project(root_path)
    # Only overwrite when symlinks were actually found or the existing list is empty.
    # Preserves explicitly registered federations when discovery returns nothing.
    if root_entry is not None and root_entry.federation != members and (members or not root_entry.federation):
        root_entry.federation = members
        upsert_project(root_entry)
    return registered


def register_all_members() -> None:
    """Register federation members for all enabled projects (idempotent)."""
    from opencode_search.core.registry import list_projects
    for entry in list_projects():
        if not entry.enabled:
            continue
        try:
            index_members(entry.path)
        except Exception as exc:
            log.warning("member-discovery %s: %s", entry.path, exc)


def expand_federation(path: str) -> list[str]:
    """Return [path] + its registered federation members (empty list for standalones)."""
    from opencode_search.core.registry import get_project
    entry = get_project(path)
    return [path] + (entry.federation if entry and entry.federation else [])


def federated_map(project_path: str, fn):  # type: ignore[no-untyped-def]
    """Run fn(GraphStore) on each member's graph.db (root first); return [(path, result)].

    Stores are opened and closed per-call; fn must not hold the store reference after return.
    Members whose graph.db does not yet exist are silently skipped.
    """
    from opencode_search.core.config import project_graph_db
    from opencode_search.graph.store import GraphStore

    out: list = []
    for p in expand_federation(project_path):
        gdb = project_graph_db(p)
        if not gdb.exists():
            continue
        gs = GraphStore(gdb)
        try:
            out.append((p, fn(gs)))
        finally:
            gs.close()
    return out
