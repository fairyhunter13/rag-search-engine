"""Project-level indexing configuration (.opencode-index.yaml/.yml).

This ports the historical Rust `indexer/src/config.rs` behavior:
- Optional config file at the project root
- index.include / index.exclude glob patterns (string or list)
- index.use_default_ignores toggle

The search engine remains dynamic: this affects which files are indexed, not
query rewriting or keyword-based ranking.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

CONFIG_FILENAMES: tuple[str, ...] = (".opencode-index.yaml", ".opencode-index.yml")


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
        return out
    return []


@dataclass(frozen=True)
class IndexConfig:
    use_default_ignores: bool = True
    exclude: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LinkedConfig:
    exclude: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    inherit: bool = False
    skip: bool = False


@dataclass(frozen=True)
class WatcherConfig:
    max_pending_files: int = 10_000


@dataclass(frozen=True)
class ProjectConfig:
    index: IndexConfig = field(default_factory=IndexConfig)
    linked: dict[str, LinkedConfig] = field(default_factory=dict)
    watcher: WatcherConfig = field(default_factory=WatcherConfig)


_cfg_cache: dict[str, tuple[float, ProjectConfig]] = {}


def load_project_config(root: Path) -> ProjectConfig:
    """Load `.opencode-index.yaml|yml` from *root*, returning defaults if missing/bad."""
    root = Path(root).expanduser().resolve()
    for name in CONFIG_FILENAMES:
        path = root / name
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        except OSError:
            continue

        cache_key = str(root)
        cached = _cfg_cache.get(cache_key)
        if cached is not None and cached[0] == st.st_mtime:
            return cached[1]

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            data = yaml.safe_load(text) or {}
        except Exception as exc:  # noqa: BLE001
            log.debug("Failed to load %s: %s", path, exc)
            cfg = ProjectConfig()
            _cfg_cache[cache_key] = (st.st_mtime, cfg)
            return cfg

        cfg = _parse_project_config(data)
        _cfg_cache[cache_key] = (st.st_mtime, cfg)
        return cfg

    return ProjectConfig()


def effective_index_config(
    project: ProjectConfig,
    linked_name: str | None = None,
    linked: ProjectConfig | None = None,
) -> IndexConfig:
    """Compute the effective IndexConfig, mirroring Rust behavior.

    `linked_name` is the key under `project.linked`. When absent, returns the
    project's own index config.
    """
    if linked_name is None:
        return project.index

    override = project.linked.get(linked_name)
    if override is None:
        return linked.index if linked is not None else IndexConfig()

    if override.skip:
        return IndexConfig(use_default_ignores=True, exclude=["**/*"], include=[])
    if override.inherit:
        return project.index

    base = linked.index if linked is not None else IndexConfig()
    return IndexConfig(
        use_default_ignores=base.use_default_ignores,
        exclude=[*base.exclude, *override.exclude],
        include=[*base.include, *override.include],
    )


def _parse_project_config(data: Any) -> ProjectConfig:
    if not isinstance(data, dict):
        return ProjectConfig()

    idx = data.get("index") if isinstance(data.get("index"), dict) else {}
    use_default_ignores = idx.get("use_default_ignores", True)
    if not isinstance(use_default_ignores, bool):
        use_default_ignores = True

    index_cfg = IndexConfig(
        use_default_ignores=use_default_ignores,
        exclude=_strings(idx.get("exclude")),
        include=_strings(idx.get("include")),
    )

    linked_raw = data.get("linked") if isinstance(data.get("linked"), dict) else {}
    linked_cfg: dict[str, LinkedConfig] = {}
    for name, raw in linked_raw.items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            continue
        linked_cfg[name] = LinkedConfig(
            exclude=_strings(raw.get("exclude")),
            include=_strings(raw.get("include")),
            inherit=bool(raw.get("inherit", False)),
            skip=bool(raw.get("skip", False)),
        )

    watcher_raw = data.get("watcher") if isinstance(data.get("watcher"), dict) else {}
    max_pending = watcher_raw.get("max_pending_files", 10_000)
    try:
        max_pending_i = int(max_pending)
    except Exception:
        max_pending_i = 10_000
    watcher_cfg = WatcherConfig(max_pending_files=max(1, max_pending_i))

    return ProjectConfig(index=index_cfg, linked=linked_cfg, watcher=watcher_cfg)


# ---------------------------------------------------------------------------
# Pattern matching (glob/fnmatch + brace expansion)
# ---------------------------------------------------------------------------


_regex_cache: dict[str, re.Pattern[str] | None] = {}


def _cached_regex(regex_str: str) -> re.Pattern[str] | None:
    cached = _regex_cache.get(regex_str)
    if regex_str in _regex_cache:
        return cached
    try:
        compiled = re.compile(regex_str)
    except re.error:
        compiled = None
    if len(_regex_cache) >= 10_000:
        _regex_cache.clear()
    _regex_cache[regex_str] = compiled
    return compiled


def expand_braces(pattern: str) -> list[str]:
    start = pattern.find("{")
    if start == -1:
        return [pattern]
    end = pattern.find("}", start + 1)
    if end == -1:
        return [pattern]
    prefix = pattern[:start]
    suffix = pattern[end + 1 :]
    inner = pattern[start + 1 : end]
    options = [o.strip() for o in inner.split(",") if o.strip()]
    out: list[str] = []
    for opt in options:
        out.extend(expand_braces(f"{prefix}{opt}{suffix}"))
    return out or [pattern]


def _glob_to_regex(pattern: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                if i + 2 < len(pattern) and pattern[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
            i += 1
            continue
        if c == "[":
            j = i + 1
            while j < len(pattern) and pattern[j] != "]":
                j += 1
            if j < len(pattern):
                out.append(pattern[i : j + 1])
                i = j + 1
                continue
        if c in ".^$+{}|()":
            out.append("\\")
        out.append(c)
        i += 1
    return "^" + "".join(out) + "$"


def _fnmatch_to_regex(pattern: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            out.append(".*")
            i += 1
            continue
        if c == "?":
            out.append(".")
            i += 1
            continue
        if c == "[":
            j = i + 1
            while j < len(pattern) and pattern[j] != "]":
                j += 1
            if j < len(pattern):
                cls = pattern[i + 1 : j]
                if cls.startswith("!"):
                    cls = "^" + cls[1:]
                out.append("[" + cls + "]")
                i = j + 1
                continue
        if c in ".^$+{}|()\\":
            out.append("\\")
        out.append(c)
        i += 1
    return "^" + "".join(out) + "$"


def match_glob(path: str, pattern: str) -> bool:
    path = path.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    regex_str = _glob_to_regex(pattern) if "**" in pattern else _fnmatch_to_regex(pattern)
    compiled = _cached_regex(regex_str)
    if compiled is None:
        return False
    return compiled.search(path) is not None


def matches_pattern(path: Path, pattern: str, root: Path) -> bool:
    rel = str(path)
    try:
        rel = str(path.resolve().relative_to(root.resolve()))
    except Exception:
        try:
            rel = str(path.relative_to(root))
        except Exception:
            rel = str(path)
    rel = rel.replace("\\", "/")
    name = (path.name or "").replace("\\", "/")

    if pattern.endswith("/"):
        dir_pat = pattern.rstrip("/").replace("\\", "/")
        if rel.startswith(dir_pat + "/"):
            return True
        parts = rel.split("/")
        pat_parts = dir_pat.split("/")
        if len(parts) >= len(pat_parts):
            for i in range(0, len(parts) - len(pat_parts) + 1):
                if parts[i : i + len(pat_parts)] == pat_parts:
                    return True

    for pat in expand_braces(pattern):
        if match_glob(rel, pat):
            return True
        if match_glob(name, pat):
            return True
    return False


def matches_any_pattern(path: Path, patterns: list[str], root: Path) -> bool:
    return any(matches_pattern(path, p, root) for p in patterns)

