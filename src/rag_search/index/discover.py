"""File discovery: walk project tree, skip ignored dirs, enforce size limits."""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pathspec

from rag_search.core.config import IGNORED_DIRS
from rag_search.core.index_config import ProjectConfig, effective_config, is_excluded

_EXCLUDE: frozenset[str] = IGNORED_DIRS | frozenset({"site-packages"})
# Public alias used by registry path filtering.
_REGISTRY_EXCLUDE_SEGMENTS = _EXCLUDE

_RSE_CFG_NAMES = (".rse-index.yaml", ".rse-index.yml")

# Discovery decision order (shared by iter_files + is_ignored_path so the drift gate and
# the watcher always agree): RSE exclude (drop) > RSE include (force-keep) > default policy
# (IGNORED_DIRS + hidden-dir, dirs only) > .gitignore (supplementary) > keep.
_GitignoreChain = tuple[tuple[Path, "pathspec.PathSpec"], ...]

_GITIGNORE_FILE_CACHE: dict[Path, tuple[float, pathspec.PathSpec]] = {}
_CFG_CACHE: dict[Path, tuple[tuple[float, ...], ProjectConfig]] = {}


def _own_gitignore_spec(gitignore_path: Path) -> pathspec.PathSpec | None:
    """Parse one .gitignore, cached and keyed on its mtime (no re-parse until it changes)."""
    try:
        mtime = gitignore_path.stat().st_mtime
    except OSError:
        return None
    cached = _GITIGNORE_FILE_CACHE.get(gitignore_path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        lines = gitignore_path.read_text(errors="ignore").splitlines()
    except OSError:
        return None
    spec = pathspec.PathSpec.from_lines("gitignore", lines)
    _GITIGNORE_FILE_CACHE[gitignore_path] = (mtime, spec)
    return spec


def _gitignore_chain_for(path: Path, root: Path) -> _GitignoreChain:
    """Ancestor .gitignore specs from root down to path's parent (each mtime-cached)."""
    chain: list[tuple[Path, pathspec.PathSpec]] = []
    own = _own_gitignore_spec(root / ".gitignore")
    if own is not None:
        chain.append((root, own))
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        return tuple(chain)
    cur = root
    for part in rel_parts[:-1]:
        cur = cur / part
        own = _own_gitignore_spec(cur / ".gitignore")
        if own is not None:
            chain.append((cur, own))
    return tuple(chain)


def _gitignore_match(full: Path, is_dir: bool, chain: _GitignoreChain) -> bool:
    """True if any ancestor-or-own .gitignore in chain matches full (each relative to its base)."""
    for base, spec in chain:
        try:
            rel = full.relative_to(base).as_posix()
        except ValueError:
            continue
        if is_dir:
            rel += "/"
        if spec.match_file(rel):
            return True
    return False


def _cached_effective_config(root: Path) -> ProjectConfig:
    """effective_config(root), cached and invalidated on the project's own config-file mtime."""
    stamps = tuple(sorted(
        (root / n).stat().st_mtime for n in _RSE_CFG_NAMES if (root / n).is_file()
    ))
    cached = _CFG_CACHE.get(root)
    if cached is not None and cached[0] == stamps:
        return cached[1]
    cfg = effective_config(root)
    _CFG_CACHE[root] = (stamps, cfg)
    return cfg


def _include_reaches(rel_parts: tuple[str, ...], patterns: list[str]) -> bool:
    """True if descending into rel_parts could still reach a path an include pattern names.

    Compares the directory's relative path against each pattern's literal prefix (the part
    before its first glob metacharacter), so pruning stays narrowed to the relevant subtree
    instead of forcing a full walk of an otherwise-excluded directory.
    """
    dir_str = "/".join(rel_parts)
    for pat in patterns:
        i = 0
        while i < len(pat) and pat[i] not in "*?[":
            i += 1
        lit_prefix = pat[:i].rstrip("/")
        if not lit_prefix or lit_prefix.startswith(dir_str) or dir_str.startswith(lit_prefix):
            return True
    return False


def _should_drop(
    full: Path, root: Path, rel_parts: tuple[str, ...], is_dir: bool,
    cfg: ProjectConfig, chain: _GitignoreChain,
) -> bool:
    """Apply the discovery decision order to one file/dir. True = drop it."""
    if cfg.exclude and is_excluded(full, cfg.exclude, root):
        return True
    if cfg.include and is_excluded(full, cfg.include, root):
        return False
    if cfg.include and is_dir and _include_reaches(rel_parts, cfg.include):
        return False
    if cfg.use_default_ignores:
        # Hidden-dir skip applies to directory segments only, never to a file's own name,
        # so tracked dotfiles (.gitignore, .eslintrc) below a visible dir still index.
        check_parts = rel_parts if is_dir else rel_parts[:-1]
        if any(part in _EXCLUDE or part.startswith(".") for part in check_parts):
            return True
    return bool(cfg.respect_gitignore and chain and _gitignore_match(full, is_dir, chain))


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


def is_code_language(lang: str) -> bool:
    """True iff lang is a tree-sitter-parseable code language (not text/data/unknown)."""
    if not lang or lang == "unknown" or lang in _TEXT_LANGS or lang in _DATA_LANGS:
        return False
    try:
        from tree_sitter_language_pack import has_language
        return bool(has_language(lang))
    except Exception:
        return False


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


def is_ignored_path(p: Path, root: Path | None = None, cfg: ProjectConfig | None = None) -> bool:
    """True if p is dropped by the discovery decision order, or is under a generated docs/ tree.

    Shares _should_drop with iter_files so the watcher (this function) and the indexer/
    source-fingerprint always agree on what counts as a real source change.
    """
    if root is None:
        return any(part in _EXCLUDE for part in p.parts)
    root = root.resolve()
    if not p.is_relative_to(root):
        return False
    rel_parts = p.relative_to(root).parts
    if not rel_parts:
        return False
    if cfg is None:
        cfg = _cached_effective_config(root)
    is_dir = p.is_dir()
    chain = _gitignore_chain_for(p, root) if cfg.respect_gitignore else ()
    if _should_drop(p, root, rel_parts, is_dir, cfg, chain):
        return True
    # Walk prefix dirs: if a "docs" segment on disk is a docgen-generated tree, ignore it.
    for i, part in enumerate(rel_parts):
        if part == "docs":
            candidate = root / Path(*rel_parts[: i + 1])
            if _is_generated_docs_dir(candidate):
                return True
    return False


def iter_files(
    root: Path, *, federation_mode: bool = False, cfg: ProjectConfig | None = None,
    include_generated_docs: bool = False,
) -> Iterator[Path]:
    """Yield indexable files under root, skipping ignored dirs and big files."""
    root = root.resolve()
    if cfg is None:
        cfg = effective_config(root)
    chain_at: dict[Path, _GitignoreChain] = {}
    root_own = _own_gitignore_spec(root / ".gitignore") if cfg.respect_gitignore else None
    chain_at[root] = ((root, root_own),) if root_own is not None else ()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        dp = Path(dirpath)
        rel_dp_parts = dp.relative_to(root).parts
        if dp == root:
            cur_chain = chain_at[root]
        else:
            parent_chain = chain_at.get(dp.parent, ())
            own = _own_gitignore_spec(dp / ".gitignore") if cfg.respect_gitignore else None
            cur_chain = (*parent_chain, (dp, own)) if own is not None else parent_chain
        chain_at[dp] = cur_chain
        dirnames[:] = [
            d for d in dirnames
            if not d.endswith(".egg-info")
            and not (not include_generated_docs and d == "docs" and _is_generated_docs_dir(dp / d))
            and not _should_drop(dp / d, root, (*rel_dp_parts, d), True, cfg, cur_chain)
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
            is_rse_cfg = fname in _RSE_CFG_NAMES
            if not is_rse_cfg and _should_drop(
                p, root, (*rel_dp_parts, fname), False, cfg, cur_chain
            ):
                continue
            lang = detect_language(p)
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if not is_rse_cfg and (size == 0 or size > _size_limit(lang)):
                continue
            yield p
