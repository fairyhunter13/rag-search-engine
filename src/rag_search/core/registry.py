"""Project registry — read/write ~/.local/share/rag-search/projects.json."""
from __future__ import annotations

import fcntl
import json
import os
import re as _re
from dataclasses import asdict
from pathlib import Path

from rag_search.core.config import REGISTRY_PATH, ProjectEntry

_LOCK_PATH = Path(str(REGISTRY_PATH) + ".lock")


def _load() -> dict:
    return json.loads(REGISTRY_PATH.read_text()) if REGISTRY_PATH.exists() else {}


def _save(data: dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOCK_PATH, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
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
    path no longer exists on disk (self-heal dead registrations)."""
    changed = False
    migrated: dict = {}
    for path, meta in data.items():
        clean = _TIER_SUFFIX.sub("", path)
        if clean != path:
            changed = True
        if "enabled" not in meta:
            meta = dict(meta, enabled=True, indexed_at=None)
            changed = True
        canon = canonicalize_path(clean)
        if canon != clean and canon not in migrated and canon not in data and Path(canon).exists():
            changed = True
            clean = canon
        if not Path(clean).exists():
            # Registered path is gone (repo deleted/moved) — drop it instead of surfacing a
            # dead project that can never be searched. Only top-level keys are pruned here;
            # `federation` member lists are untouched.
            changed = True
            continue
        migrated[clean] = meta
    if changed:
        _save(migrated)
    return migrated


def list_projects() -> list[ProjectEntry]:
    from dataclasses import fields
    data = _migrate(_load())
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


def upsert_project(entry: ProjectEntry) -> None:
    from rag_search.index.discover import is_forbidden_root
    if is_forbidden_root(Path(entry.path)):
        raise ValueError(f"refusing to register forbidden path: {entry.path}")
    data = _load()
    d = asdict(entry)
    d.pop("path")
    data[entry.path] = d
    _save(data)


def remove_project(path: str) -> bool:
    data = _load()
    if path not in data:
        return False
    del data[path]
    _save(data)
    return True
