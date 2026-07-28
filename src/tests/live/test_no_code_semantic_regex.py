"""Engine-wide guard: no code-semantic regex anywhere outside four intrinsic-mechanism files.

HR15 bans regex AND static/dynamic keyword-list / mapping-table heuristics for semantic
inference (surface-text guessing). The doctrine outlives tier 3; what changed is that the
guard no longer needs two categories to express it.

Category-B — intrinsic mechanism, explicitly exempt:
  graph/extractor.py          — tree-sitter grammar node-kind tables
  index/discover.py           — file-extension → language bootstrap
  core/registry.py            — registry path-slug plumbing (re.sub)
  core/config.py              — project-name slug plumbing (re.sub)

Category-A was the list of modules that had *eliminated* their regex and had to stay that
way: the five kb/bpre*.py files, kb/patterns.py, and server/_overview.py's service
detection. Every one of them left with tier 3, and with them the docstring's long
justification of which closed vocabularies counted as ground truth rather than heuristics
(`_V`, `_SCHEMES`, `_GRP_SFXS`, the protoc/Spring codegen-contract naming) — none of those
tables exist any more.

**The guard got stronger, not weaker.** With Category-A empty, every module in the package
except the four above is now checked, and checked against the *wide* pattern set
(compile/finditer/findall/search/match/fullmatch/sub/subn) that only Category-A used to
face — the tree-wide sweep previously screened compile/finditer alone. Measured at the
deletion: zero hits outside the four exempt files.

`_SEMANTIC_HEURISTIC_DEBT` — the registry of surviving heuristics, pinned by exact source
substring — went with them. It had been **empty since 2026-07-01**, so its guard iterated
nothing and could not fail; every entry it had ever held named a bpre_spec.py table.

test_extraction_doctrine_e2e.py's E4 used to import this module and re-invoke four of its
tests by name, so the ratchet ran "in the same run as the extraction proof". That file left
with tier 3 (its fixtures read BPRE's process_graph.db) and the re-invocation is not
re-pointed: all four names it called had already been deleted by the rewrite above, so E4
was raising AttributeError rather than ratcheting anything. The tests below carry the
doctrine on their own — they are `live`-marked and collected directly.
"""
from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_ROOT = Path(__file__).resolve().parents[2] / "rag_search"

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


def test_no_code_semantic_regex_outside_allowlist() -> None:
    """Every module but the four Category-B files must be free of the wide `re` surface.

    Replaces the Category-A/Category-B pair. The old split existed because the seven
    Category-A modules were held to the wide pattern set while the tree-wide sweep only
    screened compile/finditer; with Category-A gone there is no reason to screen the rest
    of the package less strictly than the modules that left.
    """
    violations: list[str] = []
    for py in _ROOT.rglob("*.py"):
        rel = py.relative_to(_ROOT.parent)
        mod_name = str(rel.with_suffix("")).replace("/", ".").replace("\\", ".")
        if mod_name in _CATEGORY_B_ALLOWLIST:
            continue
        if "test" in mod_name:
            continue
        try:
            src = py.read_text(errors="replace")
        except OSError:
            continue
        hits = _RE_PATTERNS.findall(src)
        if hits:
            violations.append(f"{mod_name}: {hits}")
    assert not violations, (
        "Code-semantic regex found outside the Category-B allowlist:\n"
        + "\n".join(violations)
        + "\nAdd to Category-B allowlist if this is an intrinsic mechanism (not a code heuristic)."
    )


def test_category_b_allowlist_has_no_dead_entries() -> None:
    """Every allowlisted module must exist and actually use `re` — else it is stale.

    Without this, an allowlist entry for a deleted module is a silent hole: the exemption
    survives, and a future file at that import path inherits it. That is precisely how the
    seven Category-A entries would have failed once kb/ left.
    """
    for mod_name in sorted(_CATEGORY_B_ALLOWLIST):
        src = _source(mod_name)
        assert _RE_PATTERNS.search(src), (
            f"{mod_name} is allowlisted for intrinsic `re` use but no longer uses re — "
            "drop it from _CATEGORY_B_ALLOWLIST"
        )


# Seven guards left with tier 3, each of which read a deleted module's source directly:
# bpre.py's hardcoded-gRPC-constructor check, the _SEMANTIC_HEURISTIC_DEBT registry pair
# (empty since 2026-07-01, so it iterated nothing), bpre_ast's pack-native-parser check,
# _overview's delegate-to-bpre_ast check, patterns.py's "no static _KNOWN framework map"
# check, bpre's HR23 token-accounting check, and the valueflow/resolve_rerank/bpre
# no-import-re trio. The HR23 one is worth naming: it gated that _llm_link_resolve fed
# llm_token_stats() — there is no DeepSeek call left in the repo to account for.


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
