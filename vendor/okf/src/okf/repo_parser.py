"""Standalone repo walker for OKF generator.

No OSE import. No tree-sitter. Derives top-level modules from file tree.
"""
from __future__ import annotations

from pathlib import Path

_IGNORE_DIRS = frozenset({
    ".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache",
    "dist", "build", "target", ".cargo", ".tox", ".pytest_cache", "coverage",
})
_CODE_EXTS = frozenset({
    ".py", ".js", ".ts", ".go", ".java", ".rs", ".rb", ".cpp", ".c",
    ".cs", ".php", ".swift", ".kt", ".lua", ".sh", ".bash",
})


def _should_ignore(name: str) -> bool:
    return name in _IGNORE_DIRS or name.startswith(".")


def iter_source_files(root: Path) -> list[Path]:
    result: list[Path] = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix in _CODE_EXTS:
            parts = p.relative_to(root).parts
            if not any(_should_ignore(part) for part in parts):
                result.append(p)
    return result


def top_modules(root: Path) -> list[str]:
    tops = sorted(
        d.name for d in root.iterdir()
        if d.is_dir()
        and not _should_ignore(d.name)
        and any(f.suffix in _CODE_EXTS for f in d.rglob("*") if f.is_file())
    )
    return tops or ["__root__"]


def repo_summary(root: Path) -> dict:
    files = iter_source_files(root)
    langs: dict[str, int] = {}
    for f in files:
        lang = f.suffix.lstrip(".") or "text"
        langs[lang] = langs.get(lang, 0) + 1
    return {
        "name": root.name,
        "file_count": len(files),
        "top_modules": top_modules(root),
        "languages": langs,
    }
