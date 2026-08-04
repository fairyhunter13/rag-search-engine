"""Universal language-coverage guard — GPU-free, daemon-free, no embedder.

Two structural invariants (sub-second each, live and not slow):

  1. is_code_language(lang) is True for every tree-sitter code language in our probe set.
  2. extract_symbols never raises for any supported language; it may return an empty list —
     that is correct degradation, not a failure.

The scan_file half of this module left with tier 3, and so did the third invariant, which
asserted kb/bpre.py::_source_files gated on is_code_language() rather than a hardcoded
extension list. The anti-regression guard below still covers discover.py and extractor.py,
which is where any new allowlist would now have to appear.

Standing caveat, unchanged by the deletion: clause 2's "empty list is valid" is why the
48%-of-files-yield-no-symbol gap lived undetected — an extractor returning [] for every
language passes every case here. Phase 5's TS1 is the gate that discriminates; this one
only proves nothing raises.
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
    from rag_search.index.discover import is_code_language
    assert is_code_language(lang), (
        f"is_code_language({lang!r}) returned False — "
        "tree_sitter_language_pack must have this grammar (require v>=1.9.1)"
    )


@pytest.mark.parametrize("lang,ext,snippet", _LANG_PROBES, ids=_IDS)
def test_extract_symbols_no_crash(lang: str, ext: str, snippet: str) -> None:
    """extract_symbols must not raise for any supported language (empty list is valid)."""
    from rag_search.graph.extractor import extract_symbols
    result = extract_symbols(Path(f"file.{ext}"), snippet, lang)
    assert isinstance(result, list), f"extract_symbols({lang!r}) returned {type(result).__name__}"


# The scan_file no-crash sweep is gone with tier 3: it drove kb/bpre_ast.py::scan_file over the
# same 20 probes to prove BPRE's API-surface scan never raised. There is no API-surface scan any
# more, and extract_symbols above is the only extractor left to sweep.


@pytest.mark.parametrize("lang,ext,snippet", _LANG_PROBES, ids=_IDS)
def test_extract_symbols_bounded_parity(lang: str, ext: str, snippet: str) -> None:
    """extract_symbols via run_bounded matches the direct call (HR39: bounded path, all grammars)."""
    from rag_search.graph.extractor import extract_symbols
    from rag_search.index.bounded_parse import PARSE_TIMEOUT, run_bounded
    direct = extract_symbols(Path(f"file.{ext}"), snippet, lang)
    bounded = run_bounded(extract_symbols, (Path(f"file.{ext}"), snippet, lang), path_for_log=f"file.{ext}")
    assert bounded != PARSE_TIMEOUT
    assert [(s.name, s.kind) for s in bounded] == [(s.name, s.kind) for s in direct]


# The scan_file half of HR39's bounded-parity pair goes with it. extract_symbols is now the only
# function run_bounded is asked to carry, and the parity case above still covers that contract for
# all 20 grammars.


def test_is_code_language_false_for_exclusions() -> None:
    """is_code_language must return False for text, data, and unknown/empty inputs."""
    from rag_search.index.discover import is_code_language
    # csv and po joined the data langs on 2026-07-31. Both have grammars in the pack, so this
    # answered True and they carried the 500 kB *code* cap and fed `_code_source_fingerprint` —
    # a data export was waking the graph re-derive, which is what HR38 exists to prevent.
    # vimdoc joined the text langs on 2026-08-04 for the same reason and one worse: the pack maps
    # the `.txt` *extension* to vimdoc, so `requirements.txt` and `CMakeLists.txt` were code.
    for lang in ("markdown", "rst", "text", "html", "css", "json", "yaml", "toml", "csv", "po",
                 "vimdoc", "unknown", ""):
        assert not is_code_language(lang), (
            f"is_code_language({lang!r}) must be False — "
            "text/data/unknown langs must not be treated as code"
        )


# ── Static anti-regression: no new hardcoded extension/language allowlist ─────────────────────

_SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "rag_search"
# Variable names that are permitted to hold language-name sets in the core discovery/BPRE files.
# Any NEW name indicates a new gate was added — this guard fails, preventing regression.
# The nine bpre_spec.py names this set used to carry (_FIRST_CLASS, _CALL_KINDS, _NEW_KINDS,
# _NOT_CALL, _PARADIGM_KINDS, _GRP_SFXS, _STR_KINDS, _HANDLER_KINDS, _V, _SCHEMES) left with tier 3
# along with the file that defined them.
_ALLOWED_LANG_SETS = frozenset({
    "_TEXT_LANGS", "_DATA_LANGS",  # discover.py: exclusion lists
})
# Extension lists added to discover.py after this guard existed — `_GENERATED_SUFFIXES`,
# `_GENERATED_NAMES`, `_IMAGE_SUFFIXES`, `_KEY_SUFFIXES` — are tuples, so `_EXT_SET_RE` does not
# see them, and that is a decision rather than a container-type accident. They are the extension
# bootstrap P6 names as exempt: the point where bytes first get a category, upstream of any
# language question. What this guard protects is the other thing — that once a file *has* a
# language, nothing gates on a hand-written list of which languages count as code. Any new set
# here that answers a language question belongs in `_ALLOWED_LANG_SETS` only with that argument
# made explicitly, and a frozenset of extensions still fails as it always did.
_EXT_SET_RE = re.compile(
    r"""^\s*(_[A-Z_]+)\s*[=:]\s*frozenset\s*\(\s*\{[^}]*"\.[a-z]""", re.MULTILINE
)
_LANG_SET_RE = re.compile(
    r"""^\s*(_[A-Z_]+)\s*[=:]\s*frozenset\s*\(\s*\{[^}]*"(?:go|python|java|ruby|rust|php)""",
    re.MULTILINE,
)


def test_no_new_hardcoded_lang_or_ext_allowlist_in_core() -> None:
    """Core discovery/extraction files must not introduce new frozensets of lang names or extensions.

    ead67e4 replaced a 19-extension allowlist with is_code_language(). The file that carried it
    (kb/bpre.py) is gone with tier 3, but the gate can just as easily be re-introduced in the two
    files that now decide what gets discovered and extracted — so the guard follows the behaviour,
    not the old module list.
    """
    violations: list[str] = []
    target_files = ("discover.py", "extractor.py")
    for fname in target_files:
        found = next(_SRC_ROOT.rglob(fname), None)
        # Not `continue`: a missing target means the guard silently stops guarding, which is the
        # failure mode the five deleted bpre*.py entries would have had.
        assert found is not None, f"{fname} not found under {_SRC_ROOT} — guard has no subject"
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


# The positive half of that pair asserted kb/bpre.py::_source_files called is_code_language() and
# named no extension before it. It read the file directly, so it dies with the file; the negative
# guard above is what carries the invariant forward.
