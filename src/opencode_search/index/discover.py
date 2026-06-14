"""File discovery: walk project tree, skip ignored dirs, enforce size limits."""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from opencode_search.core.config import IGNORED_DIRS
from opencode_search.core.index_config import ProjectConfig, is_excluded, load_project_config

_EXCLUDE: frozenset[str] = IGNORED_DIRS | frozenset({"site-packages"})
# Public alias used by registry path filtering.
_REGISTRY_EXCLUDE_SEGMENTS = _EXCLUDE

_EXT_LANG: dict[str, str] = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".jsx": "javascript", ".go": "go",
    ".rs": "rust", ".java": "java", ".kt": "kotlin", ".scala": "scala",
    ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
    ".rb": "ruby", ".php": "php", ".cs": "csharp", ".swift": "swift",
    ".md": "markdown", ".rst": "rst", ".txt": "text",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".sql": "sql",
    ".html": "html", ".css": "css", ".scss": "css",
}

_CODE_LANGS = frozenset({
    "python", "javascript", "typescript", "go", "rust", "java", "kotlin",
    "scala", "c", "cpp", "ruby", "php", "csharp", "swift", "bash", "sql",
})
_TEXT_LANGS = frozenset({"markdown", "rst", "text", "html", "css"})
_DATA_LANGS = frozenset({"json", "yaml", "toml"})

_SIZE_LIMITS: dict[str, int] = {
    "code": 500_000,
    "text": 200_000,
    "data": 100_000,
    "unknown": 50_000,
}
def detect_language(path: Path) -> str:
    return _EXT_LANG.get(path.suffix.lower(), "unknown")


def _size_limit(lang: str) -> int:
    if lang in _CODE_LANGS:
        return _SIZE_LIMITS["code"]
    if lang in _TEXT_LANGS:
        return _SIZE_LIMITS["text"]
    if lang in _DATA_LANGS:
        return _SIZE_LIMITS["data"]
    return _SIZE_LIMITS["unknown"]


def is_forbidden_root(path: Path) -> bool:
    """Return True if path should never be registered as a project root."""
    p = path.resolve()
    return p == Path("/tmp") or str(p).startswith("/tmp/") or (
        p.is_relative_to(Path.home() / ".cache")
    )


def is_ignored_path(p: Path, root: Path | None = None) -> bool:
    """True if any segment of p (relative to root if given) is in the canonical ignore set."""
    check = p.relative_to(root) if root else p
    return any(part in _EXCLUDE for part in check.parts)


def iter_files(
    root: Path, *, federation_mode: bool = False, cfg: ProjectConfig | None = None,
) -> Iterator[Path]:
    """Yield indexable files under root, skipping ignored dirs and big files."""
    root = root.resolve()
    if cfg is None:
        cfg = load_project_config(root)
    exc_dirs = _EXCLUDE if cfg.use_default_ignores else frozenset[str]()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        dp = Path(dirpath)
        dirnames[:] = [
            d for d in dirnames
            if d not in exc_dirs and not d.endswith(".egg-info")
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
            if cfg.exclude and is_excluded(p, cfg.exclude, root):
                continue
            lang = detect_language(p)
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size == 0 or size > _size_limit(lang):
                continue
            yield p
