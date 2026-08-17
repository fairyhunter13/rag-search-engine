"""Per-project indexing config (.rse-index.yaml|yml)."""
from __future__ import annotations

import difflib
import fnmatch
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_CONFIG_NAMES = (".rse-index.yaml", ".rse-index.yml")


class ProjectConfigError(ValueError):
    """A config file that parsed as YAML but does not describe a valid ProjectConfig."""


@dataclass(frozen=True)
class ProjectConfig:
    exclude: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    use_default_ignores: bool = True
    respect_gitignore: bool = True
    federation_exclude: list[str] = field(default_factory=list)


# Which block each field is read from. Hardcoded at both ends before, so a field added to the
# dataclass and forgotten in the loader defaulted in silence.
_FIELD_SOURCES: dict[str, tuple[str, str]] = {
    "exclude": ("index", "exclude"),
    "include": ("index", "include"),
    "use_default_ignores": ("index", "use_default_ignores"),
    "respect_gitignore": ("index", "respect_gitignore"),
    "federation_exclude": ("federation", "exclude"),
}

# Retired keys get their own message. The generic unknown-key error would tell an operator to
# fix a spelling that was never wrong, which is worse than the silent drop it replaced.
_RETIRED: dict[tuple[str, str], str] = {
    ("watcher", "max_pending_files"):
        "the watcher coalesces per project into a set, so the backlog is bounded by the "
        "project's file count and the cap enforced nothing; delete the line",
}

# A retired key's block is still accepted so its own message can fire, but is left out of the
# "known blocks" list, which is advice for writing a new file rather than a parser rule.
_LIVE_BLOCKS = {b for b, _ in _FIELD_SOURCES.values()}
_BLOCKS = _LIVE_BLOCKS | {b for b, _ in _RETIRED}


def _suggest(key: str, known: set[str]) -> str:
    close = difflib.get_close_matches(key, sorted(known), n=1)
    return f" (did you mean {close[0]!r}?)" if close else ""


def _strs(path: Path, where: str, v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
        raise ProjectConfigError(f"{path}: {where} must be a string or list of strings, got {v!r}")
    return list(v)


def _bool(path: Path, where: str, v: Any, default: bool) -> bool:
    if v is None:
        return default
    # `bool("false")` is True, so a quoted YAML boolean used to invert the setting silently.
    if not isinstance(v, bool):
        raise ProjectConfigError(f"{path}: {where} must be true or false, got {v!r}")
    return v


def _check_keys(path: Path, data: dict) -> None:
    for key in data:
        if key not in _BLOCKS:
            raise ProjectConfigError(
                f"{path}: unknown top-level key {key!r}{_suggest(key, _LIVE_BLOCKS)}. "
                f"Known blocks: {sorted(_LIVE_BLOCKS)}"
            )
    for block in _BLOCKS:
        body = data.get(block)
        if body is None:
            continue
        if not isinstance(body, dict):
            raise ProjectConfigError(f"{path}: {block!r} must be a mapping, got {body!r}")
        known = {k for b, k in _FIELD_SOURCES.values() if b == block}
        for key in body:
            if (block, key) in _RETIRED:
                raise ProjectConfigError(
                    f"{path}: {block}.{key} was retired — {_RETIRED[(block, key)]}"
                )
            if key not in known:
                raise ProjectConfigError(
                    f"{path}: unknown key {block}.{key}{_suggest(key, known)}. "
                    f"Known keys in {block!r}: {sorted(known)}"
                )


def load_project_config(root: Path) -> ProjectConfig:
    """Load .rse-index.yaml from root; defaults if absent, ProjectConfigError if wrong.

    Unparseable YAML still falls back to defaults — a half-written file caught mid-save is a
    transient the watcher recovers from. A file that parses and says something the loader does
    not understand is not transient, and dropping it silently is how `watcher.max_pending_files`
    came to be parsed, inherited and reported for months while enforcing nothing.
    """
    for name in _CONFIG_NAMES:
        path = root / name
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except Exception:
            log.warning("rse-index: bad YAML at %s, using defaults", path)
            return ProjectConfig()
        if not isinstance(data, dict):
            raise ProjectConfigError(f"{path}: top level must be a mapping, got {data!r}")
        _check_keys(path, data)
        idx = data.get("index") or {}
        fed = data.get("federation") or {}
        return ProjectConfig(
            exclude=_strs(path, "index.exclude", idx.get("exclude")),
            include=_strs(path, "index.include", idx.get("include")),
            use_default_ignores=_bool(
                path, "index.use_default_ignores", idx.get("use_default_ignores"), True),
            respect_gitignore=_bool(
                path, "index.respect_gitignore", idx.get("respect_gitignore"), True),
            federation_exclude=_strs(path, "federation.exclude", fed.get("exclude")),
        )
    return ProjectConfig()


def config_error(root: Path) -> str:
    """The config error for `root`, or "" — for a surface that must report, not raise."""
    try:
        load_project_config(root)
    except ProjectConfigError as exc:
        return str(exc)
    return ""


def effective_config(member_path: str | Path) -> ProjectConfig:
    """Return config for a project, inheriting from the federation root when applicable.

    exclude/include/federation_exclude = union(root, member); use_default_ignores and
    respect_gitignore come from member when it has its own config file, else from root.
    Standalone projects use load_project_config().

    A broken member config quarantines that member rather than raising: this runs on the
    daemon's watcher path and inside `iter_files`, so a raise here would be a fleet-wide outage
    over one project's typo. `overview(status)` surfaces it through `config_error`.
    """
    member = Path(member_path).resolve()
    member_has_own = any((member / n).is_file() for n in _CONFIG_NAMES)
    try:
        member_cfg = load_project_config(member)
    except ProjectConfigError as exc:
        log.warning("rse-index: %s — quarantining, indexing nothing under it", exc)
        return ProjectConfig(exclude=["*"])
    try:
        from rag_search.core.registry import list_projects
        root_path = next(
            (Path(e.path) for e in list_projects() if e.federation and str(member) in e.federation),
            None,
        )
    except Exception:
        return member_cfg
    if root_path is None:
        return member_cfg
    try:
        root_cfg = load_project_config(root_path)
    except ProjectConfigError as exc:
        log.warning("rse-index: federation root %s — members inherit defaults", exc)
        return member_cfg
    merged_exclude = list(dict.fromkeys(root_cfg.exclude + member_cfg.exclude))
    merged_include = list(dict.fromkeys(root_cfg.include + member_cfg.include))
    merged_fed = list(dict.fromkeys(root_cfg.federation_exclude + member_cfg.federation_exclude))
    scalars = member_cfg if member_has_own else root_cfg
    return ProjectConfig(
        exclude=merged_exclude,
        include=merged_include,
        use_default_ignores=scalars.use_default_ignores,
        respect_gitignore=scalars.respect_gitignore,
        federation_exclude=merged_fed,
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
