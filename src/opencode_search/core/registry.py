"""Project registry — read/write ~/.local/share/opencode-search/projects.json."""
from __future__ import annotations

import fcntl
import json
import os
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


def list_projects() -> list[ProjectEntry]:
    from dataclasses import fields
    data = _load()
    known = {f.name for f in fields(ProjectEntry)} - {"path"}
    return [
        ProjectEntry(path=p, **{k: v for k, v in meta.items() if k in known})
        for p, meta in data.items()
    ]


def get_project(path: str) -> ProjectEntry | None:
    meta = _load().get(path)
    return ProjectEntry(path=path, **meta) if meta else None


def upsert_project(entry: ProjectEntry) -> None:
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
