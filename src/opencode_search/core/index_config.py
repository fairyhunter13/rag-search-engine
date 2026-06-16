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


def effective_config(member_path: str | Path) -> ProjectConfig:
    """Return config for a project, inheriting from the federation root when applicable.

    exclude = union(root, member); use_default_ignores + max_pending_files come from member
    when it has its own config file, else from root. Standalone projects use load_project_config().
    """
    member = Path(member_path).resolve()
    member_has_own = any((member / n).is_file() for n in _CONFIG_NAMES)
    member_cfg = load_project_config(member)
    try:
        from opencode_search.core.registry import list_projects
        root_path = next(
            (Path(e.path) for e in list_projects() if e.federation and str(member) in e.federation),
            None,
        )
    except Exception:
        return member_cfg
    if root_path is None:
        return member_cfg
    root_cfg = load_project_config(root_path)
    merged_exclude = list(dict.fromkeys(root_cfg.exclude + member_cfg.exclude))
    scalars = member_cfg if member_has_own else root_cfg
    return ProjectConfig(
        exclude=merged_exclude,
        use_default_ignores=scalars.use_default_ignores,
        max_pending_files=scalars.max_pending_files,
    )


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
