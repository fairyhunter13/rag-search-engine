"""Project registry — read/write ~/.local/share/opencode-search/projects.json."""
from __future__ import annotations

import json
from dataclasses import asdict

from opencode_search.core.config import REGISTRY_PATH, ProjectEntry


def _load() -> dict:
    return json.loads(REGISTRY_PATH.read_text()) if REGISTRY_PATH.exists() else {}


def _save(data: dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(data, indent=2))


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
