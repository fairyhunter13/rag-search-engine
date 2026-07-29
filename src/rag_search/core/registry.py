"""Project registry — read/write ~/.local/share/rag-search/projects.json."""
from __future__ import annotations

import fcntl
import json
import os
import re as _re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from rag_search.core.config import REGISTRY_PATH, ProjectEntry

_LOCK_PATH = Path(str(REGISTRY_PATH) + ".lock")


def _load() -> dict:
    return json.loads(REGISTRY_PATH.read_text()) if REGISTRY_PATH.exists() else {}


@contextmanager
def _mutate() -> Iterator[dict]:
    """Read-modify-write the registry under one exclusive lock.

    The load used to sit *outside* the lock that guarded the store, which made every
    mutator a lost update: two writers read the same snapshot and whichever saved last
    silently dropped the other's rows. Measured rather than theorised — six uncoordinated
    writers doing 30 registrations each kept 34 of 180 (RG1). Three writers share this file
    in production (reconcile, the server, the CLI), and a dropped row is a project that is
    no longer watched or reconciled, so it rots with nothing left to report it.

    Not reentrant: flock is per open file description, so a nested `_mutate()` in the same
    process blocks on itself. Nothing nests today.
    """
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOCK_PATH, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        data = _load()
        yield data
        tmp = Path(str(REGISTRY_PATH) + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, REGISTRY_PATH)


_TIER_SUFFIX = _re.compile(r"-tier\d+$")


def canonicalize_path(path: str) -> str:
    """Expand ~ and resolve symlinks/.. to a real absolute path. Identity on empty/OSError."""
    if not path:
        return path
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return path


def resolve_registered_root(path: str) -> str:
    """Map an arbitrary path to the registered project it belongs to.

    Order matters: canonicalize first, then exact registry hit, then longest enclosing
    enabled root, else the canonicalized string. Canonicalizing before the enclosing-root
    match makes a symlinked federation member resolve to its OWN registry key (scoping
    queries to itself) rather than lexically matching the enclosing root and pulling in
    the whole federation.
    """
    if not path:
        return path
    canon = canonicalize_path(path)
    enabled = [e.path for e in list_projects() if e.enabled]
    if canon in enabled:
        return canon
    best: str | None = None
    cp = Path(canon)
    for root in enabled:
        try:
            cp.relative_to(root)
            if best is None or len(root) > len(best):
                best = root
        except ValueError:
            pass
    return best if best is not None else canon


def infer_default_project(root_paths: list[str]) -> tuple[str | None, list[str]]:
    """Infer the project a caller is in from its advertised root paths (MCP roots / cwd).

    Returns (chosen_project | None, enabled_candidates). `chosen` is set only when the roots
    imply exactly ONE enabled registered project (or only one project is enabled overall);
    otherwise None, so the caller fails loud / disambiguates rather than silently answering
    about an arbitrary projects[0]. This is what lets an unscoped tool call target the project
    the client is actually in instead of the first registry entry."""
    enabled = [e.path for e in list_projects() if e.enabled]
    cands: list[str] = []
    for rp in root_paths:
        if not rp:
            continue
        r = resolve_registered_root(rp)
        if r in enabled and r not in cands:
            cands.append(r)
    if len(cands) == 1:
        return cands[0], enabled
    if not cands and len(enabled) == 1:
        return enabled[0], enabled
    return None, enabled


def _migrate(data: dict) -> dict:
    """Normalize legacy registry format: strip tier-suffix paths, ensure required fields,
    re-key entries to their canonical real path (repairs registrations made from a raw
    symlink/relative path before query-time resolution existed), and prune entries whose
    path no longer exists on disk (self-heal dead registrations).

    Pure — it used to persist itself, which put a whole-file write on the *read* path with
    nothing holding the lock. Callers that want the normalisation kept persist it inside
    `_mutate()`; `migrated != data` says whether there is anything to keep.
    """
    migrated: dict = {}
    for path, meta in data.items():
        clean = _TIER_SUFFIX.sub("", path)
        if "enabled" not in meta:
            meta = dict(meta, enabled=True, indexed_at=None)
        canon = canonicalize_path(clean)
        if canon != clean and canon not in migrated and canon not in data and Path(canon).exists():
            clean = canon
        if not Path(clean).exists():
            # Registered path is gone (repo deleted/moved) — drop it instead of surfacing a
            # dead project that can never be searched. Only top-level keys are pruned here;
            # `federation` member lists are untouched.
            continue
        migrated[clean] = meta
    return migrated


def list_projects() -> list[ProjectEntry]:
    from dataclasses import fields
    raw = _load()
    data = _migrate(raw)
    if data != raw:
        # Re-migrate whatever is on disk *now*, not the snapshot taken before the lock —
        # another writer may have landed in between, and persisting the stale one is the
        # lost update this lock exists to prevent.
        with _mutate() as fresh:
            migrated = _migrate(dict(fresh))
            fresh.clear()
            fresh.update(migrated)
    known = {f.name for f in fields(ProjectEntry)} - {"path"}
    return [
        ProjectEntry(path=p, **{k: v for k, v in meta.items() if k in known})
        for p, meta in data.items()
    ]


def get_project(path: str) -> ProjectEntry | None:
    from dataclasses import fields
    meta = _load().get(path)
    if not meta:
        return None
    known = {f.name for f in fields(ProjectEntry)} - {"path"}
    return ProjectEntry(path=path, **{k: v for k, v in meta.items() if k in known})


def _notify_watcher() -> None:
    """Tell the running watcher the enabled set changed.

    Looked up through `sys.modules` rather than imported: a CLI process has no watcher and
    importing the daemon server there would drag uvicorn in for nothing. Inside the daemon
    the module is always already imported, so this finds it.
    """
    import contextlib
    import sys
    server = sys.modules.get("rag_search.daemon.server")
    if server is None:
        return
    # A registry write must never fail because the watcher could not be re-armed.
    with contextlib.suppress(Exception):
        server.sync_watcher()


def upsert_project(entry: ProjectEntry) -> None:
    from rag_search.index.discover import is_forbidden_root
    if is_forbidden_root(Path(entry.path)):
        raise ValueError(f"refusing to register forbidden path: {entry.path}")
    d = asdict(entry)
    d.pop("path")
    with _mutate() as data:
        prior = data.get(entry.path)
        data[entry.path] = d
    # Only on a membership change. This function also carries every index stamp, and each
    # `sync()` that finds a changed set costs a watch teardown/re-arm — so re-arming on a
    # stamp would tear the watch down hundreds of times a day for no gain.
    if prior is None or bool(prior.get("enabled", True)) != bool(entry.enabled):
        _notify_watcher()


def remove_project(path: str) -> bool:
    with _mutate() as data:
        found = path in data
        data.pop(path, None)
    if found:
        _notify_watcher()
    return found
