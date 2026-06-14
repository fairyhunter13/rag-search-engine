"""Per-project indexing config (.opencode-index.yaml|yml)."""
from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_CONFIG_NAMES = (".opencode-index.yaml", ".opencode-index.yml")


def _strs(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [x for x in v if isinstance(x, str)]
    return []


@dataclass(frozen=True)
class ProjectConfig:
    exclude: list[str] = field(default_factory=list)
    use_default_ignores: bool = True
    max_pending_files: int = 10_000


def load_project_config(root: Path) -> ProjectConfig:
    """Load .opencode-index.yaml from root; return defaults if absent or bad."""
    for name in _CONFIG_NAMES:
        path = root / name
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except Exception:
            log.warning("opencode-index: bad YAML at %s, using defaults", path)
            return ProjectConfig()
        idx = data.get("index", {}) or {}
        watch = data.get("watcher", {}) or {}
        return ProjectConfig(
            exclude=_strs(idx.get("exclude")),
            use_default_ignores=bool(idx.get("use_default_ignores", True)),
            max_pending_files=int(watch.get("max_pending_files", 10_000)),
        )
    return ProjectConfig()


def is_excluded(path: Path, patterns: list[str], root: Path) -> bool:
    """Return True if path matches any exclude glob relative to root."""
    if not patterns:
        return False
    try:
        rel = str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        rel = str(path)
    rel = rel.replace("\\", "/")
    name = path.name
    return any(
        fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat)
        for pat in patterns
    )
