"""Regression guard: live tests must never pick arbitrary real projects from the registry.

All per-project KB/overview/wiki/ask/graph/validate/docgen/okf data tests must use
sample_workspace (shop-federation + ledger-standalone). Real device projects are forbidden
as test data to keep the suite machine-agnostic and the public repo free of real paths.

This is a static source-code check — GPU-free, daemon-free, import-free.

Guards:
  1. No list_projects() picker outside registry-mechanics allowlist.
  2. No _OSE/_OSE_SRC used as a daemon data-arg outside source-read allowlist.
  3. No overview(what="projects") registry-walk + path picker outside mechanics allowlist.
  4. No unscoped search/ask (project_paths absent) while asserting on results/total,
     outside the deliberate global-fanout allowlist.
  5. No overview_tool("") or single-arg graph_tool("symbol") outside the G5-mechanics
     allowlist — those resolve to the first-enabled real registry project.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_TESTS_ROOT = Path(__file__).parents[1]   # src/tests/ — rglob covers any future subdirs
_LIVE_DIR = Path(__file__).parent         # kept for per-file context in error messages

# Files exempt from the list_projects() check — they use it for registry mechanics,
# not for picking arbitrary data projects.
_LIST_PROJECTS_ALLOWLIST = {
    "_projects.py",            # resolver module (hard-fails now, no registry fallback)
    "_sample_workspace.py",    # stale-workspace cleanup + safe_tmp_path teardown
    "conftest.py",             # sample_workspace builder + safe_tmp_path teardown
    "test_p5_server.py",       # register/remove round-trip + G5 default-resolution
    "test_p6_daemon.py",       # registry filter mechanics (test_tg2_unknown_key)
    "test_index_validity.py",  # registry precondition check (verifies sample paths are registered)
    "test_idle_stability.py",  # IS2: registry health check — must see all entries to find junk
    "test_no_real_project_in_tests.py",  # this file
}

# Files exempt from the _OSE-as-data-arg check — they use _OSE/_OSE_SRC only for
# source-file reads (vendor/, source inspect, scripts/).
_OSE_DATA_ALLOWLIST = {
    "_sample_workspace.py",  # _REPO_ROOT for vendor source reads
    "test_browser.py",       # reads dashboard.html from repo
    "test_no_code_semantic_regex.py",  # scans rag_search source tree
    "test_inference_lanes.py",         # reads scripts/*.py source
    "test_p20_capabilities.py",        # reads scripts/*.py source
    "test_okf.py",                     # _OSE_SRC for vendor/okf + sweeps.py source reads
    "test_docgen_hierarchy_e2e.py",    # _OSE_SRC for vendor/docgen + sweeps.py source reads
    "test_feature_proof.py",           # _OSE_SRC for quality.py inspect read (fp16)
    "test_p5_server.py",               # _OSE_SRC for mcp.py/ask.py/routes_chat.py source reads
    "test_no_real_project_in_tests.py",  # this file
}

# Files allowed to call overview(projects)/api/projects because they only COUNT
# the results — they never extract a path for data use.
_PROJECTS_PICKER_ALLOWLIST = {
    "test_mcp_protocol_stdio.py",    # counts ≥2, no path extraction
    "test_mcp_protocol_http.py",     # counts ≥2, no path extraction
    "test_golden_parity.py",         # presence check (overview key names)
    "test_p21_capability_parity.py", # what= coverage check
    "test_http_surface.py",          # /api/projects route status-code check
    "test_mcp_tool_matrix.py",       # overview(metrics/projects) key presence check
    "test_p5_server.py",             # G5 registry-mechanics: proves first-enabled-project rule
    "conftest.py",
    "test_no_real_project_in_tests.py",
}

# Files allowed to call search/mcp_search without project_paths while asserting results —
# they deliberately exercise the global fan-out invariant.
_UNSCOPED_SEARCH_ALLOWLIST = {
    "test_federation_architecture.py",  # test_inv2: proves member searchable globally
    "test_no_real_project_in_tests.py",
}

# Pattern: _OSE or _OSE_SRC used as a project_path= / project= / project_paths=[…] argument
# on the same logical line as a daemon call (overview/search/ask/graph/wiki/validate/enrich/okf/docgen).
_DATA_ARG_RE = re.compile(
    r"_OSE\b.*(?:project_path|project_paths|project)\s*[=\[]"
    r"|(?:project_path|project_paths|project)\s*[=\[]\s*[^\n]*_OSE\b",
)
_PROJECTS_WALK_RE = re.compile(
    r"""(?:what\s*[=:]\s*["']projects["']|/api/projects)""",
)
_PATH_PICK_RE = re.compile(
    r"""p\[["']path["']\]\s*|\.path\s+for\s+p\s+in|projects\[0\]""",
)
_RESULTS_ASSERT_RE = re.compile(r"""assert.*(?:results|total)\b""")


# Files allowed to call overview_tool("") or graph_tool("symbol_only") because they
# deliberately test the G5 first-enabled-project resolution rule (registry mechanics).
_EMPTY_PROJ_ALLOWLIST = {
    "test_p5_server.py",               # G5: empty project_path resolution mechanics
    "test_no_real_project_in_tests.py",
}
# overview_tool("") / ask_tool("","") / graph_tool("symbol") with no project_path arg
_EMPTY_PROJ_OVERVIEW_RE = re.compile(r'\boverview_tool\s*\(\s*(?:""|\'\')')
_SINGLE_ARG_GRAPH_RE = re.compile(r'\bgraph_tool\s*\(\s*"[^"]*"\s*\)')


def _iter_py_files():
    return sorted(_TESTS_ROOT.rglob("*.py"))


def test_no_list_projects_picker_outside_allowlist():
    """No live test file may call list_projects() unless it is on the registry-mechanics allowlist."""
    violations: list[str] = []
    for f in _iter_py_files():
        if f.name in _LIST_PROJECTS_ALLOWLIST:
            continue
        src = f.read_text(encoding="utf-8")
        if "list_projects(" in src:
            # Count occurrences for context
            count = src.count("list_projects(")
            violations.append(f"{f.name}: {count} call(s)")
    assert not violations, (
        "These files call list_projects() outside the allowlist — "
        "replace with sample_workspace fixtures:\n" + "\n".join(violations)
    )


def test_no_ose_as_data_arg_outside_allowlist():
    """_OSE must not be passed as a project_path/project arg to daemon endpoints outside the allowlist."""
    violations: list[str] = []
    for f in _iter_py_files():
        if f.name in _OSE_DATA_ALLOWLIST:
            continue
        src = f.read_text(encoding="utf-8")
        for i, line in enumerate(src.splitlines(), 1):
            if "_OSE" in line and _DATA_ARG_RE.search(line):
                violations.append(f"{f.name}:{i}: {line.strip()[:80]}")
    assert not violations, (
        "_OSE used as daemon data arg outside allowlist — "
        "use sample_workspace fixtures instead:\n" + "\n".join(violations)
    )


def test_no_registry_projects_walk_and_pick():
    """No file may call overview(projects)/api/projects AND then extract a path for reuse.

    A 'walker' reads all registered projects and picks one arbitrarily — it binds
    to whatever real projects happen to be on the device. Count-only accesses are
    allowlisted (they don't extract a path; the data stays opaque).
    """
    violations: list[str] = []
    for f in _iter_py_files():
        if f.name in _PROJECTS_PICKER_ALLOWLIST:
            continue
        src = f.read_text(encoding="utf-8")
        if not _PROJECTS_WALK_RE.search(src):
            continue
        if _PATH_PICK_RE.search(src):
            lines = [
                f"  line {i}: {ln.strip()[:80]}"
                for i, ln in enumerate(src.splitlines(), 1)
                if _PATH_PICK_RE.search(ln)
            ]
            violations.append(
                f"{f.name} — walks overview(projects) and picks a path:\n" + "\n".join(lines)
            )
    assert not violations, (
        "Files that iterate overview(projects) and extract a path bind to arbitrary "
        "real registry projects. Use sample_workspace fixtures instead, or add to "
        "_PROJECTS_PICKER_ALLOWLIST with justification:\n" + "\n".join(violations)
    )


def test_no_unscoped_search_asserting_results():
    """No file may call mcp_search/search_tool without project_paths while asserting results/total.

    An unscoped search fans out to ALL registered projects — it binds to real-device
    content. The deliberate global-fanout invariant tests are allowlisted.
    """
    violations: list[str] = []
    for f in _iter_py_files():
        if f.name in _UNSCOPED_SEARCH_ALLOWLIST:
            continue
        src = f.read_text(encoding="utf-8")
        lines = src.splitlines()
        for i, line in enumerate(lines):
            if not re.search(r"""\b(?:mcp_search|search_tool)\s*\(""", line):
                continue
            if "project_paths" in line:
                continue
            window = "\n".join(lines[i:min(i + 6, len(lines))])
            if _RESULTS_ASSERT_RE.search(window):
                violations.append(f"{f.name}:{i + 1}: {line.strip()[:80]}")
    assert not violations, (
        "These calls use mcp_search/search_tool without project_paths while asserting "
        "results — they bind to real-device projects. Scope to sample_workspace paths "
        "or add to _UNSCOPED_SEARCH_ALLOWLIST with justification:\n"
        + "\n".join(violations)
    )


def test_no_empty_project_path_daemon_call():
    """Guard 5: no overview_tool("") or bare graph_tool("symbol") outside the G5-mechanics allowlist.

    overview_tool("") and graph_tool(symbol_only) both resolve to the first-enabled registry
    project — a real device project.  Only the deliberate G5 resolution-mechanics tests in
    test_p5_server.py are permitted to use this pattern.
    """
    violations: list[str] = []
    for f in _iter_py_files():
        if f.name in _EMPTY_PROJ_ALLOWLIST:
            continue
        src = f.read_text(encoding="utf-8")
        for i, line in enumerate(src.splitlines(), 1):
            if _EMPTY_PROJ_OVERVIEW_RE.search(line) or _SINGLE_ARG_GRAPH_RE.search(line):
                violations.append(f"{f.name}:{i}: {line.strip()[:80]}")
    assert not violations, (
        "overview_tool('') / graph_tool(symbol_only) resolve to the first-enabled REAL registry "
        "project — use sample_workspace fixture paths instead, or add to _EMPTY_PROJ_ALLOWLIST "
        "with a justification comment:\n" + "\n".join(violations)
    )
