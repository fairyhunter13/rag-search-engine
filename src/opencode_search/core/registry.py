"""Project registry — read/write ~/.local/share/opencode-search/projects.json."""
from __future__ import annotations

import fcntl
import json
import os
import re as _re
from dataclasses import asdict
from pathlib import Path

from opencode_search.core.config import REGISTRY_PATH, ProjectEntry

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


def _migrate(data: dict) -> dict:
    """Normalize legacy registry format: strip tier-suffix paths, ensure required fields."""
    changed = False
    migrated: dict = {}
    for path, meta in data.items():
        clean = _TIER_SUFFIX.sub("", path)
        if clean != path:
            changed = True
        if "enabled" not in meta:
            meta = dict(meta, enabled=True, indexed_at=None)
            changed = True
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
    from opencode_search.index.discover import is_forbidden_root
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
