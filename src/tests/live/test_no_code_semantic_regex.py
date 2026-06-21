"""Engine-wide guard: assert no code-semantic regex/keyword/mapping in Category-A paths.

Category-A sites (eliminated — must stay regex-free):
  kb/bpre.py, kb/bpre_ast.py — structural BPRE detection
  kb/patterns.py              — framework labelling (now LLM)
  server/_overview.py         — service detection (now bpre_ast Pass A)

Category-B sites (intrinsic mechanism — explicitly exempt):
  graph/extractor.py          — tree-sitter grammar node-kind tables
  index/discover.py           — file-extension → language bootstrap
  core/registry.py            — registry path-slug plumbing (re.sub)
  core/config.py              — project-name slug plumbing (re.sub)
"""
from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_ROOT = Path(__file__).resolve().parents[3] / "opencode_search"

_CATEGORY_A = [
    "opencode_search.kb.bpre",
    "opencode_search.kb.bpre_ast",
    "opencode_search.kb.patterns",
    "opencode_search.server._overview",
]

_CATEGORY_B_ALLOWLIST = {
    "opencode_search.graph.extractor",
    "opencode_search.index.discover",
    "opencode_search.core.registry",
    "opencode_search.core.config",
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
    src = _source("opencode_search.kb.bpre")
    # No hardcoded constructor prefix/suffix patterns (now discovered from pb.go)
    assert "NewCartServiceClient" not in src, "Hardcoded gRPC constructor name found"
    assert "RegisterCartServer" not in src, "Hardcoded gRPC registrar name found"
    # No static publish-verb keyword set
    assert '"Publish"' not in src or "bpre_ast" in src, "Hardcoded Publish verb found"


def test_bpre_ast_uses_tree_sitter_only() -> None:
    """H4 guard: bpre_ast must use pack-native has_language/get_parser; no _TS_LANG; no re."""
    src = _source("opencode_search.kb.bpre_ast")
    assert "_TS_LANG" not in src, "bpre_ast must NOT import _TS_LANG (removed in H4)"
    assert "has_language" in src, "bpre_ast must use has_language() from the pack (H4)"
    assert "get_parser" in src, "bpre_ast must use get_parser() from the pack (H4)"
    assert "import re" not in src, "bpre_ast must not import re"
    assert "re.compile" not in src, "bpre_ast must not use re.compile"


def test_extractor_has_no_hardcoded_lang_dicts() -> None:
    """H1/H2 guard: graph/extractor.py must not contain _TS_LANG/_DEF_KINDS/_CALL_NODE."""
    src = _source("opencode_search.graph.extractor")
    assert "_TS_LANG" not in src, "extractor must not define _TS_LANG (removed in H1)"
    assert "_DEF_KINDS" not in src, "extractor must not define _DEF_KINDS (removed in H1)"
    assert "_CALL_NODE" not in src, "extractor must not define _CALL_NODE (removed in H2)"
    assert "_STRUCTURE_KIND_MAP" in src, "extractor must use _STRUCTURE_KIND_MAP (H1)"
    assert "process" in src, "extractor must call process() (H1)"


def test_discover_uses_pack_language_detection() -> None:
    """H3 guard: index/discover.py must use detect_language_from_path; no _EXT_LANG."""
    src = _source("opencode_search.index.discover")
    assert "_EXT_LANG" not in src, "discover must not define _EXT_LANG (removed in H3)"
    assert "detect_language_from_path" in src, "discover must use detect_language_from_path (H3)"


def test_overview_detect_services_uses_bpre_ast() -> None:
    """server/_overview.py _detect_services must delegate to bpre_ast, not re.finditer."""
    src = _source("opencode_search.server._overview")
    assert "bpre_ast" in src, "_detect_services must use kb.bpre_ast"
    assert "re.finditer" not in src, "_detect_services must not use re.finditer"
    assert "import re" not in src, "_overview.py must not import re"


def test_patterns_no_static_framework_map() -> None:
    """kb/patterns.py must not contain the _KNOWN static map; framework labels via LLM."""
    src = _source("opencode_search.kb.patterns")
    assert "_KNOWN" not in src, "Static _KNOWN framework map must be removed"
    assert "deepseek" in src.lower() or "llm" in src.lower(), (
        "patterns.py must use LLM for framework labelling"
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
        "opencode_search.kb.valueflow",
        "opencode_search.kb.resolve_rerank",
        "opencode_search.kb.llm_escalation",
    ):
        src = _source(mod_name)
        # match standalone "import re" or "import re\n" but not "import rerank_*"
        assert "\nimport re\n" not in src and not src.startswith("import re\n"), (
            f"{mod_name} must not import re"
        )
        assert "re.compile" not in src, f"{mod_name} must not call re.compile"
