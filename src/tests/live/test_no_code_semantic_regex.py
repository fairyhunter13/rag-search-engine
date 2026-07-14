"""Engine-wide guard: assert no code-semantic regex/keyword/mapping in Category-A paths.

Category-A sites (eliminated — must stay regex-free):
  kb/bpre.py, kb/bpre_ast.py         — structural BPRE detection
  kb/bpre_spec.py                     — tree-sitter node-kind maps + closed HTTP-verb/scheme sets (ground truth)
  kb/bpre_generic.py, kb/bpre_paradigms.py — generic/paradigm HTTP client-vs-route classification
  kb/patterns.py                      — framework labelling (now LLM)
  server/_overview.py                 — service detection (now bpre_ast Pass A)

Category-B sites (intrinsic mechanism — explicitly exempt):
  graph/extractor.py          — tree-sitter grammar node-kind tables
  index/discover.py           — file-extension → language bootstrap
  core/registry.py            — registry path-slug plumbing (re.sub)
  core/config.py              — project-name slug plumbing (re.sub)

HR15 bans regex AND static/dynamic keyword-list / mapping-table heuristics for semantic
inference (surface-text guessing). Closed protocol vocabularies (e.g. the fixed HTTP method
set `bpre_spec._V` and the protocol/URI-scheme set `bpre_spec._SCHEMES`), tree-sitter
node-kind maps (`_CALL_KINDS`/`_NEW_KINDS`/`_HANDLER_KINDS`/etc.), and protocol/framework
codegen-contract naming bound to structural facts are ground truth, not heuristics, and are
allowed — e.g. `bpre_ast.py`'s protoc `New*Client`/`Register*Server`/`*Client`-receiver
discovery (scoped to `.pb.go` codegen output only, feeding a structural dict lookup at call
sites) and Spring's `*Mapping` annotation vocabulary (paired with structural argument/route
extraction), the PHP `*Client` constructor check gated on `cls_name[:-6] in s.proto_services`
(an actual discovered proto service, not a guess), and `_GRP_SFXS` (gRPC codegen-contract
suffixes, likewise gated on a discovered `proto_services` match, not a bare guess).

`_SEMANTIC_HEURISTIC_DEBT` below is now **empty** (2026-07-01): the last surviving entry,
`bpre_spec._LANG_SPECS`/`_DEFAULT_SPEC` (15 per-language method-name keyword tables consumed
by the generic fallback path for every non-first-class language), was retired in favor of ONE
universal structural classifier — URL-path anchor + `_has_handler_arg` handler-shape + `_V`
verb ground-truth + gRPC proto-binding (`_GRP_SFXS` against discovered `proto_services`) +
`_SCHEMES` receiver-text provenance for non-verb client idioms (C# `GetAsync`, Elixir `get!`,
Swift `dataTask`, …) — covering all 299 tree-sitter *code* grammars by construction, not by
per-language enumeration. BPRE's Category-A modules are now fully structural/ground-truth; the
registry may only ever shrink from here — a new, unlisted heuristic is a regression.
"""
from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_ROOT = Path(__file__).resolve().parents[2] / "rag_search"

_CATEGORY_A = [
    "rag_search.kb.bpre",
    "rag_search.kb.bpre_ast",
    "rag_search.kb.bpre_spec",
    "rag_search.kb.bpre_generic",
    "rag_search.kb.bpre_paradigms",
    "rag_search.kb.patterns",
    "rag_search.server._overview",
]

# Known surviving (b)-category name-matching heuristics (HR15 debt). Each entry names an
# *exact* source substring expected to still be present. A migration (Part 3) that removes
# a heuristic must delete its entry here — the registry only shrinks, never grows.
#
# bpre_ast.py's protoc/Spring codegen-contract naming (New*Client/Register*Server/*Mapping/
# proto-bound *Client) and bpre_spec.py's _GRP_SFXS were reclassified 2026-07-01 as ground
# truth, not debt — see the module docstring above — because each site is scoped to codegen
# output or paired with a structural fact (proto_services / .pb.go discovery), not a bare
# surface-text guess. The last true entry, `_LANG_SPECS`/`_DEFAULT_SPEC` (per-language HTTP
# method-name tables), was retired the same day by the universal structural classifier
# (bpre_generic.py/bpre_paradigms.py) — the registry is now empty.
_SEMANTIC_HEURISTIC_DEBT: dict[str, tuple[str, ...]] = {}

_CATEGORY_B_ALLOWLIST = {
    "rag_search.graph.extractor",
    "rag_search.index.discover",
    "rag_search.core.registry",
    "rag_search.core.config",
}

_RE_PATTERNS = re.compile(r"\bre\.(compile|finditer|findall|search|match|fullmatch|sub|subn)\b")


def _source(mod_name: str) -> str:
    mod = importlib.import_module(mod_name)
    return inspect.getsource(mod)


def test_no_code_semantic_regex_in_category_a() -> None:
    """All Category-A modules must contain zero re.compile / re.finditer / re.sub calls."""
    violations: list[str] = []
    for mod_name in _CATEGORY_A:
        src = _source(mod_name)
        hits = _RE_PATTERNS.findall(src)
        if hits:
            violations.append(f"{mod_name}: {hits}")
    assert not violations, "Code-semantic regex found in Category-A modules:\n" + "\n".join(violations)


def test_category_b_allowlist_is_exhaustive() -> None:
    """No module outside Category-A or Category-B allowlist may use re.compile/finditer."""
    violations: list[str] = []
    for py in _ROOT.rglob("*.py"):
        rel = py.relative_to(_ROOT.parent)
        mod_name = str(rel.with_suffix("")).replace("/", ".").replace("\\", ".")
        if mod_name in {*_CATEGORY_A, *_CATEGORY_B_ALLOWLIST}:
            continue
        if "test" in mod_name:
            continue
        try:
            src = py.read_text(errors="replace")
        except OSError:
            continue
        hits = re.findall(r"\bre\.(compile|finditer)\b", src)
        if hits:
            violations.append(f"{mod_name}: {hits}")
    assert not violations, (
        "Unexpected re.compile/finditer outside Category-A/B boundary:\n"
        + "\n".join(violations)
        + "\nAdd to Category-B allowlist if this is an intrinsic mechanism (not a code heuristic)."
    )


def test_bpre_no_hardcoded_api_surface_patterns() -> None:
    """kb/bpre.py must not contain hardcoded gRPC constructor patterns or method verb sets."""
    src = _source("rag_search.kb.bpre")
    # No hardcoded constructor prefix/suffix patterns (now discovered from pb.go)
    assert "NewCartServiceClient" not in src, "Hardcoded gRPC constructor name found"
    assert "RegisterCartServer" not in src, "Hardcoded gRPC registrar name found"
    # No static publish-verb keyword set
    assert '"Publish"' not in src or "bpre_ast" in src, "Hardcoded Publish verb found"


def test_semantic_heuristic_debt_registry_is_accurate() -> None:
    """Each pinned debt entry must still be present — proves the registry matches reality.

    When a Part-3 migration removes a heuristic, delete its entry here in the same change;
    a stale entry (heuristic gone but still pinned) fails loudly instead of silently drifting.
    """
    violations: list[str] = []
    for mod_name, needles in _SEMANTIC_HEURISTIC_DEBT.items():
        src = _source(mod_name)
        for needle in needles:
            if needle not in src:
                violations.append(f"{mod_name}: pinned debt {needle!r} no longer present — remove from registry")
    assert not violations, "\n".join(violations)


def test_no_new_semantic_heuristics_beyond_debt_registry() -> None:
    """bpre_generic.py / bpre_paradigms.py must not grow their own name-matching tables.

    They classify via the universal structural signals (URL-anchor, _has_handler_arg,
    _V, _GRP_SFXS/proto_services, _SCHEMES provenance) — any new per-module keyword table
    is an unlisted heuristic and must route through structural resolution + the residue
    ladder (resolve_rerank -> bpre._llm_link_resolve) instead.
    """
    for mod_name in ("rag_search.kb.bpre_generic", "rag_search.kb.bpre_paradigms"):
        src = _source(mod_name)
        assert "_LANG_SPECS" not in src, f"{mod_name} must not define its own language-spec table"
        assert "frozenset({" not in src, f"{mod_name} must not define a new keyword frozenset"


def test_bpre_ast_uses_tree_sitter_only() -> None:
    """H4 guard: bpre_ast must use pack-native has_language/get_parser; no _TS_LANG; no re."""
    src = _source("rag_search.kb.bpre_ast")
    assert "_TS_LANG" not in src, "bpre_ast must NOT import _TS_LANG (removed in H4)"
    assert "has_language" in src, "bpre_ast must use has_language() from the pack (H4)"
    assert "get_parser" in src, "bpre_ast must use get_parser() from the pack (H4)"
    assert "import re" not in src, "bpre_ast must not import re"
    assert "re.compile" not in src, "bpre_ast must not use re.compile"


def test_extractor_has_no_hardcoded_lang_dicts() -> None:
    """H1/H2 guard: graph/extractor.py must not contain _TS_LANG/_DEF_KINDS/_CALL_NODE."""
    src = _source("rag_search.graph.extractor")
    assert "_TS_LANG" not in src, "extractor must not define _TS_LANG (removed in H1)"
    assert "_DEF_KINDS" not in src, "extractor must not define _DEF_KINDS (removed in H1)"
    assert "_CALL_NODE" not in src, "extractor must not define _CALL_NODE (removed in H2)"
    assert "_STRUCTURE_KIND_MAP" in src, "extractor must use _STRUCTURE_KIND_MAP (H1)"
    assert "process" in src, "extractor must call process() (H1)"


def test_discover_uses_pack_language_detection() -> None:
    """H3 guard: index/discover.py must use detect_language_from_path; no _EXT_LANG."""
    src = _source("rag_search.index.discover")
    assert "_EXT_LANG" not in src, "discover must not define _EXT_LANG (removed in H3)"
    assert "detect_language_from_path" in src, "discover must use detect_language_from_path (H3)"


def test_overview_detect_services_uses_bpre_ast() -> None:
    """server/_overview.py _detect_services must delegate to bpre_ast, not re.finditer."""
    src = _source("rag_search.server._overview")
    assert "bpre_ast" in src, "_detect_services must use kb.bpre_ast"
    assert "re.finditer" not in src, "_detect_services must not use re.finditer"
    # Match the actual `re` module import, not the substring — otherwise legitimate lines like
    # `from ...registry import resolve_registered_root` ("import re"solve) false-positive.
    assert not any(ln.strip() in ("import re", "import re as re") for ln in src.splitlines()), (
        "_overview.py must not import re"
    )


def test_patterns_no_static_framework_map() -> None:
    """kb/patterns.py must not contain the _KNOWN static map; framework labels via LLM."""
    src = _source("rag_search.kb.patterns")
    assert "_KNOWN" not in src, "Static _KNOWN framework map must be removed"
    assert "deepseek" in src.lower() or "llm" in src.lower(), (
        "patterns.py must use LLM for framework labelling"
    )


def test_bpre_link_resolve_tokens_are_accounted() -> None:
    """HR23 guard: bpre.py's Tier-2 edge-linking DeepSeek call must feed llm_token_stats().

    _llm_link_resolve previously called deepseek_extract without accumulation, making its
    token spend invisible to overview(what='metrics') — a gap in the DIKW token-economy
    budget that this test prevents from regressing.
    """
    src = _source("rag_search.kb.bpre")
    assert "_accumulate_llm_tokens" in src, "bpre.py must import _accumulate_llm_tokens"
    assert '_accumulate_llm_tokens(usage, "bpre_link")' in src, (
        "_llm_link_resolve must accumulate its DeepSeek usage under the bpre_link namespace"
    )


def test_no_skip_markers_in_live_suite() -> None:
    """Policy guard: the live suite must contain zero pytest.skip/xfail/skipif markers.

    A reintroduced skip fails this test immediately, making the no-skip policy
    machine-enforceable.  Complements the gate's -x --strict-markers invocation.
    """
    suite_dir = Path(__file__).parent
    violations: list[str] = []
    markers = ("pytest.skip(", "pytest.xfail(", "@pytest.mark.xfail", "@pytest.mark.skipif")
    for py in sorted(suite_dir.glob("*.py")):
        if py.name == Path(__file__).name:
            continue  # this file itself contains the marker strings as literals
        src = py.read_text(errors="replace")
        for m in markers:
            if m in src:
                violations.append(f"{py.name}: contains '{m}'")
    assert not violations, (
        "Live suite must contain NO skip/xfail/skipif markers (no-skip policy):\n"
        + "\n".join(violations)
    )


def test_no_import_re_in_resolution_path() -> None:
    """Zero-vocab doctrine: Tier-1.5/1.75/2 resolution modules must not use re."""
    for mod_name in (
        "rag_search.kb.valueflow",
        "rag_search.kb.resolve_rerank",
        "rag_search.kb.bpre",
    ):
        src = _source(mod_name)
        # match standalone "import re" or "import re\n" but not "import rerank_*"
        assert "\nimport re\n" not in src and not src.startswith("import re\n"), (
            f"{mod_name} must not import re"
        )
        assert "re.compile" not in src, f"{mod_name} must not call re.compile"
