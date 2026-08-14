"""File discovery: walk project tree, skip ignored dirs, enforce size limits."""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pathspec

from rag_search.core.config import IGNORED_DIRS
from rag_search.core.index_config import ProjectConfig, effective_config, is_excluded

# One name for one rule: an alias here is how two callers come to believe they filter differently.
_EXCLUDE: frozenset[str] = IGNORED_DIRS | frozenset({"site-packages"})

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


_TEXT_SNIFF_BYTES = 8192


def _has_text_bytes(path: Path) -> bool:
    """True if path's first 8 kB contain no NUL byte — git's text/binary test.

    Consulted for every file that survives every cheaper rule, whatever language the pack
    named it (see `_should_drop`: restricting this to "unknown" let sklearn pickles through
    on an extension collision). The read is the last thing discovery does and the chunker is
    about to read the same bytes, so it measured at +3% of a warm walk.

    An unreadable file is reported as text, matching the `stat()` failure above:
    "cannot evaluate the policy" must not become "drop it", or a deleted file stops
    surviving is_ignored_path and never gets purged from the index.
    """
    try:
        with path.open("rb") as fh:
            return b"\x00" not in fh.read(_TEXT_SNIFF_BYTES)
    except OSError:
        return True


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
    # Sits beside is_generated_path, and below `cfg.include`, on the same convention: an explicit
    # RSE include is the user saying this file matters in their own repo, and overriding that
    # silently would be the more surprising failure. Everything else drops.
    if not is_dir and is_secret_path(full.name):
        return True
    # Third of the same species, same placement and the same `cfg.include` override: a text-encoded
    # image is an image, and `_has_text_bytes` — the rule that drops every other image — cannot see
    # that, because SVG really is text. Dropped here rather than by size, which lets the small ones
    # through and is not the objection anyway.
    if not is_dir and is_image_path(full.name):
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
    # a churn loop, caught live on the largest workspace's 143-172 kB diagram specs reappearing one reconcile
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
    lang = detect_language(full)
    if size == 0 or size > _size_limit(lang):
        return True
    # Last rule, and only for files no grammar and no shebang could name: are these bytes
    # even text? Nothing upstream ever asked. A .png under the 50 kB `unknown` cap was read
    # as UTF-8 with errors="replace", chunked, embedded and returned as a search hit — one
    # sampled chunk was literally `# Onboarding Docs/image-1.png\n\xd9g\xc3E_\x16...`. That
    # is 174,039 chunks of mojibake fleet-wide (measured 2026-07-30), which is a precision
    # problem before it is a cost one: those vectors sit in every KNN scan and can outrank
    # real code.
    #
    # NUL in the first 8 kB is git's own text test, derived from the file instead of from a
    # list of extensions to keep sufficient. Still deliberately narrow in *what it tests*: it
    # does NOT try to catch minified/bundled JS (screening bundles by line geometry was measured
    # on 2026-07-28 and refuted — it deleted 54.4% of the fleet graph), and it keeps non-ASCII
    # *text* such as gettext .po catalogues, which a printable-ASCII-ratio rule wrongly rejects.
    #
    # It used to be `lang == "unknown" and ...`, on the reasoning that a file the pack could name
    # should be trusted and should not pay the read. Both halves were wrong, measured 2026-07-30:
    #   - A named language does not mean text bytes. `.pkl` collides with Apple's **Pkl** config
    #     language, so five sklearn pickles (`\x80\x04\x95...MinMaxScaler`) were named `pkl`,
    #     given the 500 kB *code* cap, and indexed. At 719 bytes each that was 5 junk chunks by
    #     luck; the cap is what makes it a hole, since a 400 kB model artifact chunks in full.
    #   - The read costs +3% of a warm discovery walk (1,010 files), because every file that gets
    #     this far is about to be opened and read in full by the chunker anyway.
    # Widening it drops exactly 7 files across the whole fleet (66,911 walked): those 5 pickles
    # plus two UTF-16LE files whose every other byte is NUL. Dropping those two is deliberate and
    # matches git, which also calls UTF-16 binary — the reader has no BOM handling, so they were
    # stored as UTF-8-with-replace mojibake, and absent from the index is more honest than wrong
    # in it. A BOM carve-out would be a hand-written list whose whole effect is to keep junk.
    return not _has_text_bytes(full)


# H3: non-parseable text/data formats kept explicitly; code = any language
# the pack can parse (detected via has_language() in _size_limit).
_TEXT_LANGS: frozenset[str] = frozenset({"markdown", "rst", "text", "html", "css", "vimdoc"})
# The pack ships a grammar for prose and data formats too, so `is_code_language` answered True for
# them and they took the 500 kB *code* cap and fed `_code_source_fingerprint` — editing a
# `requirements.txt` (the pack maps `.txt` to vimdoc) or a CSV export could wake a graph re-derive,
# which is what HR38 exists to prevent. Listing them here reclassifies rather than excludes, so a
# 40-line hand-written test CSV stays searchable under the data cap.
#
# The criterion for joining this set is prose-or-data, **never** "100 % dark": groovy is 2,065
# files at 100 % dark and is unambiguously code, while scss holds 673 symbols and reclassifying it
# would delete extraction that works. All 40 fleet languages were audited on that criterion and
# only vimdoc moved — docs/decisions/2026-08-04-the-language-axis-was-already-universal.md, and
# docs/decisions/2026-07-31-corpus-hygiene.md for csv/po. Adding one moves H1's coverage
# denominator (`graph/store.py`), so the ratio shifts without the graph having changed.
_DATA_LANGS: frozenset[str] = frozenset({"json", "yaml", "toml", "csv", "po"})

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
# Provenance, kept because it is why the list exists: the largest workspace's own SvelteKit dashboard
# (its `wiki/` directory — not the deleted kb/wiki.py) regenerated src/lib/*.generated.js on
# every build and looped the reconstruct cascade. That cascade left with tier 3; the drift signal
# it shared with the graph re-derive did not.
#
# Extended 2026-07-31 with three more species of the same thing, 70 files / 2,738 chunks: the
# dependency lockfile (`.lock`, plus `go.sum` and `package-lock.json`, which carry no suffix that
# would identify them), the minified bundle, and the sourcemap. Each is a build artifact whose
# input is versioned beside it, so indexing it stores the same information twice and re-deriving
# on its churn is work for nothing. `.js.map`/`.css.map` rather than a bare `.map`: all 22
# sourcemaps in the fleet are one of those two, and `.map` alone is wider than the evidence.
# Only `.min.js` moves HR38's fingerprint (javascript is code today, the others are not) — and
# that is the half worth moving.
_GENERATED_SUFFIXES: tuple[str, ...] = (
    "_pb2.py", "_pb2_grpc.py", ".pb.go", ".pb.gw.go", ".pb.cc", ".pb.h",
    ".g.dart", ".freezed.dart",
    ".lock", ".min.js", ".min.css", ".js.map", ".css.map",
)
# Matched whole, because these two identify themselves by their entire name and `endswith` on a
# bare `sum`/`json` would take real source with it.
_GENERATED_NAMES: tuple[str, ...] = ("go.sum", "package-lock.json")


def is_generated_path(rel: str | os.PathLike) -> bool:
    """True iff rel names a machine-generated code file (codegen/build output).

    Conservative — matches only unambiguous generated markers (``*.generated.*``, ``*.gen.*``,
    protobuf/dart codegen suffixes) so hand-written source is never misclassified. One drift
    signal reads it now — sweeps._code_source_fingerprint (HR38) — so regenerating a derived
    file never wakes the graph re-derive. The second reader it used to name, bpre._bpre_code_sig,
    left with tier 3; the guard is still load-bearing because the re-derive it gates is the
    expensive half that survived.

    Screening by line geometry instead (S10, mean line >= 200 chars, to reject minified bundles)
    was measured on 2026-07-28 and rejected: it deleted 54.4% of the fleet graph and *raised* the
    cross-family ratio it targeted, 5.968% -> 6.144%, because bundles are internally consistent
    and the top offenders included jQuery and codemirror. Screen on provenance, not shape.
    """
    name = os.path.basename(str(rel))
    if ".generated." in name or ".gen." in name:
        return True
    return name in _GENERATED_NAMES or name.endswith(_GENERATED_SUFFIXES)


# Text-encoded images. `.svg` and `.drawio` are XML, so `_has_text_bytes` waves them through as
# text and a grammar parses them, but their content is geometry — path data, transforms, base64
# blobs — and nothing in it answers a question about code. They are the single largest bucket in
# the corpus after HTML: 9,173 files, 21,059 chunks, 4.96 % of the fleet, measured 2026-07-31.
#
# A tuple, deliberately: `test_no_new_hardcoded_lang_or_ext_allowlist_in_core` fails on any new
# module-level `frozenset({".x"…})` in this file, and that guard is right to. This is the
# extension bootstrap HR15 names as exempt — the point at which bytes first get a category — and
# not a language gate, which is what the guard is protecting `is_code_language()` from becoming.
_IMAGE_SUFFIXES: tuple[str, ...] = (".svg", ".drawio", ".drawio.xml")


def is_image_path(rel: str | os.PathLike) -> bool:
    """True iff rel names a text-encoded image — an image that happens to be spelled in XML."""
    return os.path.basename(str(rel)).lower().endswith(_IMAGE_SUFFIXES)


# Key material and credential stores, added 2026-07-31 on the same evidence standard the
# docstring below sets for the dotenv family. A live PEM *private* key was in the vector store —
# `certs/privkey.pem`, whose first line is `-----BEGIN` — together with three `htpasswd` files:
# 5 files, 12 chunks. Small mass, and mass is not the argument; a private key in a store that
# `search` returns from and the dashboard pastes into a chat prompt is the argument.
#
# `.pem` and `htpasswd` are the measured instances. The rest are formats *defined* to carry key
# material, so a file with one of these names has nothing else it could be. `.crt`, `.cer` and
# `.pub` are deliberately absent: those are the public halves, published on purpose, and dropping
# them would be exclusion by association with the word "certificate".
_KEY_SUFFIXES: tuple[str, ...] = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")


def is_secret_path(rel: str | os.PathLike) -> bool:
    """True iff rel names a file that holds credentials or key material by convention.

    Measured 2026-07-31: 509 chunks from these files were in the vector store across the fleet,
    including `mysql-credentials.env` and `instance-secrets.env`. Being in the store means being
    returned by `search` and pasteable into a `claude -p` chat prompt, so this is an exposure
    rather than a coverage question — which is why the fix is subtractive where the surrounding
    universality work is additive.

    They reached the index because nothing excluded them: `IGNORED_DIRS` names `.env` as a
    *directory*, and `_should_drop`'s hidden-name skip is deliberately restricted to directory
    segments so tracked dotfiles (`.gitignore`, `.eslintrc`) still index. Both are correct;
    neither covers a *file* called `.env`.

    Deliberately the dotenv family only, matched on the name the format itself defines: `.env`,
    anything under it (`.env.local`, `.env.production`), and the `<name>.env` spelling
    (`mysql-credentials.env`). `.env.example` is included even though it usually holds
    placeholders — the cost of dropping a template is a little lost documentation, and the cost
    of keeping a mis-named real one is a leaked credential. This is not a general secrets
    classifier and should not grow into one by guesswork; a new pattern comes with a measured
    instance of it, the same standard `_GENERATED_SUFFIXES` is held to.
    """
    name = os.path.basename(str(rel))
    if name == ".env" or name.startswith(".env.") or name.endswith(".env"):
        return True
    low = name.lower()
    # `lstrip(".")` because the canonical Apache spelling is `.htpasswd` and the three found on
    # this fleet were bare `htpasswd`; a prefix test anchored on either one alone misses the other.
    return low.endswith(_KEY_SUFFIXES) or low.lstrip(".").startswith("htpasswd")


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
