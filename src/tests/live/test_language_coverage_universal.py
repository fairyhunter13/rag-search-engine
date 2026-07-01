"""Universal language-coverage guard — GPU-free, daemon-free, no embedder.

Three structural invariants (sub-second each, live and not slow):

  1. is_code_language(lang) is True for every tree-sitter code language in our probe set.
  2. extract_symbols + scan_file never raise for any supported language; they may return
     empty results — that is correct degradation, not a failure.
  3. _source_files in kb/bpre.py uses is_code_language() and contains no hardcoded extension
     allowlist (prevents re-introducing the gate that ead67e4 removed).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

# 20-language cross-paradigm probe set — C-family, JVM, scripting, functional, systems.
# Each entry: (tree-sitter language name, file-extension hint, minimal valid snippet).
# All 20 are present in tree-sitter-language-pack >=1.9.1 (c_sharp renamed "csharp" in 1.12.1).
_LANG_PROBES: list[tuple[str, str, str]] = [
    ("go",         "go",    "package main\nfunc Hello() {}\n"),
    ("python",     "py",    "def hello():\n    pass\n"),
    ("typescript", "ts",    "function hello(): void {}\n"),
    ("javascript", "js",    "function hello() {}\n"),
    ("php",        "php",   "<?php\nfunction hello() {}\n"),
    ("ruby",       "rb",    "def hello\nend\n"),
    ("rust",       "rs",    "fn hello() {}\n"),
    ("csharp",     "cs",    "class C { void Hello() {} }\n"),
    ("kotlin",     "kt",    "fun hello() {}\n"),
    ("scala",      "scala", "object S { def hello(): Unit = {} }\n"),
    ("swift",      "swift", "func hello() {}\n"),
    ("dart",       "dart",  "void hello() {}\n"),
    ("elixir",     "ex",    "defmodule M do\n  def hello, do: :ok\nend\n"),
    ("lua",        "lua",   "function hello() end\n"),
    ("r",          "r",     "hello <- function() NULL\n"),
    ("julia",      "jl",    "function hello() end\n"),
    ("perl",       "pl",    "sub hello { }\n"),
    ("groovy",     "groovy","def hello() {}\n"),
    ("clojure",    "clj",   "(defn hello [] nil)\n"),
    ("haskell",    "hs",    "hello = ()\n"),
]
_IDS = [t[0] for t in _LANG_PROBES]


@pytest.mark.parametrize("lang,ext,snippet", _LANG_PROBES, ids=_IDS)
def test_is_code_language_true(lang: str, ext: str, snippet: str) -> None:
    """is_code_language must return True for every tree-sitter code language in the probe set."""
    from opencode_search.index.discover import is_code_language
    assert is_code_language(lang), (
        f"is_code_language({lang!r}) returned False — "
        "tree_sitter_language_pack must have this grammar (require v>=1.9.1)"
    )


@pytest.mark.parametrize("lang,ext,snippet", _LANG_PROBES, ids=_IDS)
def test_extract_symbols_no_crash(lang: str, ext: str, snippet: str) -> None:
    """extract_symbols must not raise for any supported language (empty list is valid)."""
    from opencode_search.graph.extractor import extract_symbols
    result = extract_symbols(Path(f"file.{ext}"), snippet, lang)
    assert isinstance(result, list), f"extract_symbols({lang!r}) returned {type(result).__name__}"


@pytest.mark.parametrize("lang,ext,snippet", _LANG_PROBES, ids=_IDS)
def test_scan_file_no_crash(lang: str, ext: str, snippet: str) -> None:
    """scan_file must not raise for any supported language (None or empty surface is valid)."""
    from opencode_search.kb.bpre_ast import ApiSurface, scan_file
    surf = ApiSurface()
    result = scan_file(f"file.{ext}", snippet, lang, surf)
    assert result is None or hasattr(result, "http_clients"), (
        f"scan_file({lang!r}) returned unexpected type: {type(result).__name__}"
    )


@pytest.mark.parametrize("lang,ext,snippet", _LANG_PROBES, ids=_IDS)
def test_extract_symbols_bounded_parity(lang: str, ext: str, snippet: str) -> None:
    """extract_symbols via run_bounded matches the direct call (HR39: bounded path, all grammars)."""
    from opencode_search.graph.extractor import extract_symbols
    from opencode_search.index.bounded_parse import PARSE_TIMEOUT, run_bounded
    direct = extract_symbols(Path(f"file.{ext}"), snippet, lang)
    bounded = run_bounded(extract_symbols, (Path(f"file.{ext}"), snippet, lang), path_for_log=f"file.{ext}")
    assert bounded != PARSE_TIMEOUT
    assert [(s.name, s.kind) for s in bounded] == [(s.name, s.kind) for s in direct]


@pytest.mark.parametrize("lang,ext,snippet", _LANG_PROBES, ids=_IDS)
def test_scan_file_bounded_parity(lang: str, ext: str, snippet: str) -> None:
    """scan_file via run_bounded matches the direct call (HR39: bounded path, all grammars)."""
    from opencode_search.index.bounded_parse import PARSE_TIMEOUT, run_bounded
    from opencode_search.kb.bpre_ast import ApiSurface, scan_file
    direct = scan_file(f"file.{ext}", snippet, lang, ApiSurface())
    bounded = run_bounded(scan_file, (f"file.{ext}", snippet, lang, ApiSurface()), path_for_log=f"file.{ext}")
    assert bounded != PARSE_TIMEOUT
    if direct is None:
        assert bounded is None
    else:
        assert bounded.http_clients == direct.http_clients
        assert bounded.http_routes == direct.http_routes


def test_is_code_language_false_for_exclusions() -> None:
    """is_code_language must return False for text, data, and unknown/empty inputs."""
    from opencode_search.index.discover import is_code_language
    for lang in ("markdown", "rst", "text", "html", "css", "json", "yaml", "toml", "unknown", ""):
        assert not is_code_language(lang), (
            f"is_code_language({lang!r}) must be False — "
            "text/data/unknown langs must not be treated as code"
        )


# ── Static anti-regression: no new hardcoded extension/language allowlist ─────────────────────

_SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "opencode_search"
# Variable names that are permitted to hold language-name sets in the core discovery/BPRE files.
# Any NEW name indicates a new gate was added — this guard fails, preventing regression.
_ALLOWED_LANG_SETS = frozenset({
    "_TEXT_LANGS", "_DATA_LANGS",  # discover.py: exclusion lists
    "_FIRST_CLASS",                 # bpre_spec.py: bespoke opt-in tier (not a gate)
    "_CALL_KINDS", "_NEW_KINDS", "_NOT_CALL", "_PARADIGM_KINDS", "_GRP_SFXS", "_STR_KINDS",
    "_HANDLER_KINDS",               # bpre_spec.py: structural handler-shape node kinds
    "_V",                           # bpre_spec.py: HTTP verb set (not language names)
    "_SCHEMES",                     # bpre_spec.py: protocol/URI-scheme set (not language names)
})
_EXT_SET_RE = re.compile(
    r"""^\s*(_[A-Z_]+)\s*[=:]\s*frozenset\s*\(\s*\{[^}]*"\.[a-z]""", re.MULTILINE
)
_LANG_SET_RE = re.compile(
    r"""^\s*(_[A-Z_]+)\s*[=:]\s*frozenset\s*\(\s*\{[^}]*"(?:go|python|java|ruby|rust|php)""",
    re.MULTILINE,
)


def test_no_new_hardcoded_lang_or_ext_allowlist_in_core() -> None:
    """Core BPRE/discovery files must not introduce new frozensets of lang names or file extensions.

    ead67e4 replaced the 19-extension allowlist in _source_files with is_code_language().
    This guard prevents that gate from being re-introduced under a new name.
    """
    violations: list[str] = []
    target_files = (
        "discover.py", "extractor.py",
        "bpre.py", "bpre_ast.py", "bpre_spec.py", "bpre_generic.py", "bpre_paradigms.py",
    )
    for fname in target_files:
        found = next(_SRC_ROOT.rglob(fname), None)
        if found is None:
            continue
        src = found.read_text()
        for m in _EXT_SET_RE.finditer(src):
            if m.group(1) not in _ALLOWED_LANG_SETS:
                violations.append(f"{fname}: {m.group(1)!r} — frozenset of file extensions detected")
        for m in _LANG_SET_RE.finditer(src):
            if m.group(1) not in _ALLOWED_LANG_SETS:
                violations.append(f"{fname}: {m.group(1)!r} — frozenset of language names detected")
    assert not violations, (
        "New hardcoded lang/extension allowlist in core discovery/BPRE code:\n"
        + "\n".join(violations)
        + "\nAll language gating must use is_code_language() or has_language()."
    )


def test_source_files_uses_is_code_language() -> None:
    """kb/bpre.py::_source_files must call is_code_language() (not a hardcoded ext set)."""
    bpre = next(_SRC_ROOT.rglob("bpre.py"), None)
    assert bpre is not None, "kb/bpre.py not found"
    src = bpre.read_text()
    assert "is_code_language" in src, (
        "_source_files must gate on is_code_language() — hardcoded extension list forbidden"
    )
    assert '".go"' not in src or src.index('".go"') > src.index("is_code_language"), (
        '".go" appears before is_code_language — extension allowlist may have been re-introduced'
    )
