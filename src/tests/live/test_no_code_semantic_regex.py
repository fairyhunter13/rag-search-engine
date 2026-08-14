r"""Engine-wide guard: no code-semantic regex anywhere outside two intrinsic-mechanism files.

No regex, and no static or dynamic keyword-list / mapping-table heuristic, may be used for
semantic inference — surface-text guessing about what code *means*. Structure comes from
tree-sitter or it does not come at all.

Category-B — intrinsic mechanism, explicitly exempt:
  core/registry.py            — tier-suffix strip on registry keys (_re.compile)
  core/config.py              — project-name slug plumbing (re.sub)

An exemption for a module that does not use the thing exempted is a hole: a regex added there
later inherits the pass. `test_category_b_allowlist_has_no_dead_entries` below is what keeps the list honest.

**The detector is alias-aware.** `core/registry.py` writes `import re as _re`, and a `\bre\.(…)`
pattern cannot see its `_re.compile` — so any module could go invisible by renaming the import.
Binding names are read off the import statement instead (`_re_hits`).

Every module in the package except those two is checked, against the wide pattern set
(compile/finditer/findall/search/match/fullmatch/sub/subn). Measured zero hits outside the
exempt files, re-measured 2026-07-28 with the alias-aware detector.
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
    "rag_search.core.registry",
    "rag_search.core.config",
}

_RE_FUNCS = "compile|finditer|findall|search|match|fullmatch|sub|subn"
_IMPORT_RE = re.compile(r"^[ \t]*import[ \t]+re(?:[ \t]+as[ \t]+(\w+))?[ \t]*$", re.MULTILINE)
_FROM_RE = re.compile(r"^[ \t]*from[ \t]+re[ \t]+import[ \t]+(.+)$", re.MULTILINE)


def _re_hits(src: str) -> list[str]:
    """Every regex call in `src`, found through whatever name `re` is bound to here.

    `import re as _re` defeated a fixed `\\bre\\.` pattern silently — the module went on using
    regex and the guard went on passing. Names come off the import statement instead.
    """
    hits = [f"from re import {names.strip()}" for names in _FROM_RE.findall(src)]
    for alias in _IMPORT_RE.findall(src):
        name = alias or "re"
        hits += [f"{name}.{fn}" for fn in re.findall(rf"\b{name}\.({_RE_FUNCS})\b", src)]
    return hits


def _source(mod_name: str) -> str:
    mod = importlib.import_module(mod_name)
    return inspect.getsource(mod)


def test_no_code_semantic_regex_outside_allowlist() -> None:
    """Every module but the Category-B files must be free of the wide `re` surface.

    One list, one strictness: screening most of the package for compile/finditer alone, and only
    a named few for the wide set, means the modules nobody thought to name are the least guarded.
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
        hits = _re_hits(src)
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
    survives, and a future file at that import path inherits it.
    """
    dead = [m for m in sorted(_CATEGORY_B_ALLOWLIST) if not _re_hits(_source(m))]
    assert not dead, (
        f"{dead} are allowlisted for intrinsic `re` use but no longer use re — drop them from "
        "_CATEGORY_B_ALLOWLIST. (Report every one: this used to assert per-entry and stopped at "
        "the first, so two dead entries read as one.)"
    )


# A guard that names one module's source can only outlive that module as a vacuous pass or an
# import error. Prefer the tree-wide scans above, which find the next offender rather than the
# last one; name a module only when the property really is that module's alone.


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
