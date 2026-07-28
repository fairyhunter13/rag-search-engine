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


def gitignore_chain_for_dir(dirpath: Path, root: Path) -> _GitignoreChain:
    """The chain that applies to every direct child of `dirpath` — identical for all of them.

    `_gitignore_chain_for` only ever reads `path`'s ancestors (`rel_parts[:-1]`), so a loop over
    a directory's files rebuilds the same tuple once per file, re-joining a `.gitignore` Path at
    every level. A tree walk should call this once per directory and pass the result down.
    """
    return _gitignore_chain_for(dirpath / "_", root)


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
    # Machine-generated code files are derived build/codegen output: never index or watch them,
    # so regenerating them can't wake the indexer/embedder or the drift gate (mirrors the
    # generated-docs/ tree skip in iter_files). Explicit RSE `include` above still wins.
    if not is_dir and is_generated_path(full.name):
        return True
    if cfg.use_default_ignores:
        # Hidden-dir skip applies to directory segments only, never to a file's own name,
        # so tracked dotfiles (.gitignore, .eslintrc) below a visible dir still index.
        check_parts = rel_parts if is_dir else rel_parts[:-1]
        if any(part in _EXCLUDE or part.startswith(".") for part in check_parts):
            return True
    if cfg.respect_gitignore and chain and _gitignore_match(full, is_dir, chain):
        return True
    if is_dir:
        return False
    # Size is the last rule on purpose: it costs a stat(), and everything above rejects for free.
    #
    # It lived only in iter_files, so the watcher's screen (is_ignored_path) and discovery's
    # disagreed on exactly this one rule. A file past the cap was watched and indexed, then
    # reported orphaned by the set-drift check and purged, then re-indexed on its next write —
    # a churn loop, caught live on redacted-name-10's 143-172 kB diagram specs reappearing one reconcile
    # pass after being purged. Before the drift check existed the same disagreement simply
    # accumulated: 42,952 chunks the engine had decided were not indexable, still being searched.
    # Shared here for the reason is_generated_path was: the watcher, the indexer and the drift
    # gate have to apply one rule, and the only way to guarantee that is one implementation.
    try:
        size = full.stat().st_size
    except OSError:
        # Cannot evaluate the policy, so this is not a policy drop. Returning True here breaks
        # deletion: `_index_files` filters its input through is_ignored_path, and a removed file
        # has to survive that filter to be purged from the index at all. Caught by
        # test_daemon_incremental_reindex_purges_deleted_file, which is why that gate exists.
        return False
    return size == 0 or size > _size_limit(detect_language(full))


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
    """Return the pack's language id for path (H3: detect_language_from_path, 306+ langs).

    S7: an extensionless file falls back to its shebang. `Makefile`, `Dockerfile`, `.env` and a
    bare `script` all return None from path detection — measured against pack 1.12.1 — so every
    such file was invisible to the graph. The shebang answers for the executable ones (python,
    bash, node); Dockerfile and Makefile have no content signature and stay unknown, which is
    the honest result rather than a filename table.

    The read is bounded to the first line and only happens when path detection has already
    failed, so the hot discovery loop pays nothing for the overwhelming majority of files.
    """
    try:
        from tree_sitter_language_pack import (
            detect_language_from_content,
            detect_language_from_path,
        )
        lang = detect_language_from_path(str(path))
        if lang:
            return lang
        with path.open("rb") as fh:
            head = fh.readline(256)
        if head.startswith(b"#!"):
            lang = detect_language_from_content(head.decode("utf-8", errors="replace"))
        return lang if lang else "unknown"
    except Exception:
        return "unknown"


# S8: language families for call-edge resolution. A call can only bind to a definition the
# caller's grammar could actually reach, and the unit that governs that is not the language but
# the family: a `.ts` file legitimately calls a `.js` export, a `.vue` SFC's <script> block *is*
# javascript, and a Blade template is PHP. Anything not named here is its own family, which is
# the strict case — the table only ever *widens* what resolution accepts.
#
# This is a taxonomy, not a heuristic: it states which grammars share a module system, the same
# way _STRUCTURE_KIND_MAP states which StructureKinds are functions. HR15 bans guessing *meaning*
# from surface text; it does not ban knowing that TypeScript compiles to JavaScript.
_LANGUAGE_FAMILY: dict[str, str] = {
    "javascript": "js", "typescript": "js", "jsx": "js", "tsx": "js",
    "vue": "js", "svelte": "js", "astro": "js", "html": "js",
    "c": "c", "cpp": "c", "objc": "c", "cuda": "c",
    "php": "php", "blade": "php",
}


def language_family(lang: str) -> str:
    """The resolution family `lang` belongs to; a language with no relatives is its own family."""
    return _LANGUAGE_FAMILY.get(lang, lang)


def is_code_language(lang: str) -> bool:
    """True iff lang is a tree-sitter-parseable code language (not text/data/unknown)."""
    if not lang or lang == "unknown" or lang in _TEXT_LANGS or lang in _DATA_LANGS:
        return False
    try:
        from tree_sitter_language_pack import has_language
        return bool(has_language(lang))
    except Exception:
        return False


# Machine-generated code files: derived build/codegen output (protobuf stubs, dart codegen,
# SvelteKit dashboards, *.generated.*). tree-sitter parses them as real code, but regenerating
# them is NOT source drift — treating it as such re-derives the graph for nothing.
# Provenance, kept because it is why the list exists: redacted-name-10-project's own SvelteKit dashboard
# (its `wiki/` directory — not the deleted kb/wiki.py) regenerated src/lib/*.generated.js on
# every build and looped the reconstruct cascade. That cascade left with tier 3; the drift signal
# it shared with the graph re-derive did not.
_GENERATED_SUFFIXES: tuple[str, ...] = (
    "_pb2.py", "_pb2_grpc.py", ".pb.go", ".pb.gw.go", ".pb.cc", ".pb.h",
    ".g.dart", ".freezed.dart",
)


def is_generated_path(rel: str | os.PathLike) -> bool:
    """True iff rel names a machine-generated code file (codegen/build output).

    Conservative — matches only unambiguous generated markers (``*.generated.*``, ``*.gen.*``,
    protobuf/dart codegen suffixes) so hand-written source is never misclassified. One drift
    signal reads it now — sweeps._code_source_fingerprint (HR38) — so regenerating a derived
    file never wakes the graph re-derive. The second reader it used to name, bpre._bpre_code_sig,
    left with tier 3; the guard is still load-bearing because the re-derive it gates is the
    expensive half that survived.
    """
    name = os.path.basename(str(rel))
    if ".generated." in name or ".gen." in name:
        return True
    return name.endswith(_GENERATED_SUFFIXES)


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


def is_ignored_path(
    p: Path, root: Path | None = None, cfg: ProjectConfig | None = None,
    *, is_dir: bool | None = None, chain: _GitignoreChain | None = None,
) -> bool:
    """True if p is dropped by the discovery decision order.

    Shares _should_drop with iter_files so the watcher (this function) and the indexer/
    source-fingerprint always agree on what counts as a real source change.

    `is_dir` and `chain` are pure hoists for callers already walking a tree: both are constant
    across a directory's children, and recomputing them per file costs a `stat()` and a full
    ancestor rebuild each time. Passing them changes no decision — omit them and they are
    derived exactly as before.
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
    if is_dir is None:
        is_dir = p.is_dir()
    if not cfg.respect_gitignore:
        chain = ()  # a caller-supplied chain must never override an opted-out config
    elif chain is None:
        chain = _gitignore_chain_for(p, root)
    return _should_drop(p, root, rel_parts, is_dir, cfg, chain)


def iter_files(
    root: Path, *, federation_mode: bool = False, cfg: ProjectConfig | None = None,
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
            # No size test here any more: _should_drop owns it, so the walk and the watcher
            # cannot drift apart. `is_rse_cfg` keeps its exemption by bypassing that call.
            yield p
