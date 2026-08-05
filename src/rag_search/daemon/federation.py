"""Federation: discover symlinked sub-repos and register them."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from rag_search.core.config import IGNORED_DIRS

log = logging.getLogger(__name__)


def _looks_like_repo(target: Path) -> bool:
    from rag_search.index.discover import iter_files
    return next(iter_files(target), None) is not None


def discover_members(root_path: str) -> list[str]:
    """Return resolved paths of nested symlinked dirs (any depth) that look like repos.

    Deduped (order-preserved): the same repo is often symlinked from several locations
    under the root, which would otherwise store the member N times in root.federation and
    make every consumer that walks it do N× the work — index_members here, and every
    expand_federation caller: search (mcp), ask, routes_chat, graph_handler, _overview,
    validate. The two tier-3 walkers this used to name (sweeps burst-enrich, _bpre_source_sig)
    are gone; the dedup still matters because search fans out over all 194 members per query.
    expand_federation also dedups, as defense.
    """
    from rag_search.core.config import is_federation_excluded
    root = Path(root_path).resolve()
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
                if is_federation_excluded(str(target)):
                    dirs.remove(d)
                    continue
                if _looks_like_repo(target):
                    members.append(str(target))
                dirs.remove(d)
    except OSError:
        pass
    return list(dict.fromkeys(members))  # dedup, order-preserved


def index_members(root_path: str) -> int:
    """Register all discovered federation members; persist root.federation. Returns count newly registered."""
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import get_project, upsert_project

    members = discover_members(root_path)
    member_set = set(members)
    registered = 0
    for m in members:
        if get_project(m) is None:
            upsert_project(ProjectEntry(path=m, enabled=True))
            registered += 1
    root_entry = get_project(root_path)
    if root_entry is not None:
        old_set = set(root_entry.federation or [])
        # Disable members that discovery no longer finds (only when discovery returned
        # something — guards against transient OSError wiping valid federations).
        if members:
            for removed in old_set - member_set:
                removed_entry = get_project(removed)
                if removed_entry is not None and removed_entry.enabled:
                    removed_entry.enabled = False
                    upsert_project(removed_entry)
        # Only overwrite when symlinks were actually found or the existing list is empty.
        # Preserves explicitly registered federations when discovery returns nothing.
        if root_entry.federation != members and (members or not root_entry.federation):
            root_entry.federation = members
            upsert_project(root_entry)
    return registered


def register_all_members() -> None:
    """Register federation members for all enabled projects, and un-enable excluded ones.

    The second half is not tidiness. `RSE_FEDERATION_EXCLUDE` reaches only the processes that
    were given it, and the daemon's copy comes from a systemd drop-in — so *every* out-of-daemon
    entry point (the CLI, the live suite, a script) runs `index_members` with the variable unset,
    rediscovers the excluded members and upserts them back as `enabled=True`. `index_members`
    cannot undo that on the next daemon start: exclusion filters at *discovery* time, so those
    paths are no longer members of anything, and a row nothing discovers is a row nothing
    revisits. Measured 2026-07-30 on this fleet: 58 `_worktrees` rows enabled and 58 stale entries
    in one root's `federation` list, both re-created after the drop-in was already installed and
    in force, with the daemon having restarted since.

    So the exclusion is applied to the registry directly, by the one process that reliably holds
    the value, at the one moment it already walks every row. `enabled=False` (rather than deleting
    the row) is deliberate: it is what `index_members` does for a member discovery drops, it keeps
    `indexed_at` so the store is still reclaimable, and it is reversible by an operator who
    decides the path belongs after all. Guarded by FE9.
    """
    from rag_search.core.config import is_federation_excluded
    from rag_search.core.registry import list_projects, upsert_project
    for entry in list_projects():
        if not entry.enabled:
            continue
        if is_federation_excluded(entry.path):
            log.info("federation-excluded, disabling: %s", entry.path)
            entry.enabled = False
            upsert_project(entry)
            continue
        try:
            index_members(entry.path)
        except Exception as exc:
            log.warning("member-discovery %s: %s", entry.path, exc)


def expand_federation(path: str) -> list[str]:
    """Return [path] + its registered federation members, deduped, order-preserved.

    Two symlinks resolving to the same target (or stale duplicate registry state) could
    otherwise put the same member path in the union twice, double-processing its store.
    """
    from rag_search.core.registry import get_project
    entry = get_project(path)
    members = entry.federation if entry and entry.federation else []
    return list(dict.fromkeys([path, *members]))


def searchable_stores(entries: list) -> list[str]:  # type: ignore[type-arg]
    """Every store a query can actually reach: enabled roots plus every federation member.

    A *disabled* member is still searched when a root federates to it — `expand_federation`
    never re-checks `enabled` — so filtering on that flag would miss the largest stores.

    Registry-driven by construction, which is the boundary to keep in mind when this feeds a
    fleet-wide count: an index dir with no registry row is invisible here. That is deliberate.
    Orphaned store dirs are Guard 6's problem (`test_no_real_project_in_tests.py`) and the
    `safe_tmp_path` fixture's, not a convergence signal — conflating the two is what once left
    orphan dirs holding `stale_stores` above zero for reasons no operator could trace back.
    """
    roots = {e.path for e in entries if e.enabled}
    return sorted(roots | {m for e in entries for m in (e.federation or [])})


def federated_map(project_path: str, fn):  # type: ignore[no-untyped-def]
    """Run fn(GraphStore) on each member's graph.db (root first); return [(path, result)].

    Stores are opened and closed per-call; fn must not hold the store reference after return.
    Members whose graph.db does not yet exist are silently skipped.
    """
    from rag_search.core.config import project_graph_db
    from rag_search.graph.store import GraphStore

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
