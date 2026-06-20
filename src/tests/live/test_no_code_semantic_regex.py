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
    """kb/bpre_ast.py must import _TS_LANG from extractor and must NOT import re."""
    src = _source("opencode_search.kb.bpre_ast")
    assert "_TS_LANG" in src, "bpre_ast must reuse _TS_LANG from graph.extractor"
    assert "import re" not in src, "bpre_ast must not import re"
    assert "re.compile" not in src, "bpre_ast must not use re.compile"


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
