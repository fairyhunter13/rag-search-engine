"""File discovery: walk project tree, skip ignored dirs, enforce size limits."""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from opencode_search.core.config import IGNORED_DIRS
from opencode_search.core.index_config import ProjectConfig, effective_config, is_excluded

_EXCLUDE: frozenset[str] = IGNORED_DIRS | frozenset({"site-packages"})
# Public alias used by registry path filtering.
_REGISTRY_EXCLUDE_SEGMENTS = _EXCLUDE

# H3: non-parseable text/data formats kept explicitly; code = any language
# the pack can parse (detected via has_language() in _size_limit).
_TEXT_LANGS: frozenset[str] = frozenset({"markdown", "rst", "text", "html", "css"})
_DATA_LANGS: frozenset[str] = frozenset({"json", "yaml", "toml"})

_SIZE_LIMITS: dict[str, int] = {
    "code": 500_000,
    "text": 200_000,
    "data": 100_000,
    "unknown": 50_000,
}


def detect_language(path: Path) -> str:
    """Return the pack's language id for path (H3: detect_language_from_path, 306+ langs)."""
    try:
        from tree_sitter_language_pack import detect_language_from_path
        lang = detect_language_from_path(str(path))
        return lang if lang else "unknown"
    except Exception:
        return "unknown"


def _size_limit(lang: str) -> int:
    if lang in _TEXT_LANGS:
        return _SIZE_LIMITS["text"]
    if lang in _DATA_LANGS:
        return _SIZE_LIMITS["data"]
    if lang and lang != "unknown":
        try:
            from tree_sitter_language_pack import has_language
            if has_language(lang):
                return _SIZE_LIMITS["code"]
        except Exception:
            pass
    return _SIZE_LIMITS["unknown"]


def is_forbidden_root(path: Path) -> bool:
    """Return True if path should never be registered as a project root."""
    p = path.resolve()
    return p == Path("/tmp") or str(p).startswith("/tmp/") or (
        p.is_relative_to(Path.home() / ".cache")
    )


def _is_generated_docs_dir(p: Path) -> bool:
    """True if p is a docgen-generated docs/ tree (contains _meta/provenance.json)."""
    return p.is_dir() and (p / "_meta" / "provenance.json").exists()


def is_ignored_path(p: Path, root: Path | None = None) -> bool:
    """True if any segment of p is in the canonical ignore set, or p is under a generated docs/ tree."""
    if root and not p.is_relative_to(root):
        return False
    check = p.relative_to(root) if root else p
    if any(part in _EXCLUDE for part in check.parts):
        return True
    # Walk prefix dirs: if a "docs" segment on disk is a docgen-generated tree, ignore it.
    base = root or Path()
    parts = check.parts
    for i, part in enumerate(parts):
        if part == "docs":
            candidate = base / Path(*parts[: i + 1])
            if _is_generated_docs_dir(candidate):
                return True
    return False


def iter_files(
    root: Path, *, federation_mode: bool = False, cfg: ProjectConfig | None = None,
) -> Iterator[Path]:
    """Yield indexable files under root, skipping ignored dirs and big files."""
    root = root.resolve()
    if cfg is None:
        cfg = effective_config(root)
    exc_dirs = _EXCLUDE if cfg.use_default_ignores else frozenset[str]()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        dp = Path(dirpath)
        dirnames[:] = [
            d for d in dirnames
            if d not in exc_dirs
            and not d.endswith(".egg-info")
            and not (d == "docs" and _is_generated_docs_dir(dp / d))
        ]
        if federation_mode:
            dirnames[:] = [
                d for d in dirnames
                if not (dp / d).is_symlink()
                or (dp / d).resolve().is_relative_to(root)
            ]
        for fname in filenames:
            p = dp / fname
            if federation_mode and p.is_symlink() and not p.resolve().is_relative_to(root):
                continue
            is_ose_cfg = fname in {".opencode-index.yaml", ".opencode-index.yml"}
            if not is_ose_cfg and cfg.exclude and is_excluded(p, cfg.exclude, root):
                continue
            lang = detect_language(p)
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if not is_ose_cfg and (size == 0 or size > _size_limit(lang)):
                continue
            yield p
