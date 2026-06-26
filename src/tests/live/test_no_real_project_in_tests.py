"""Regression guard: live tests must never pick arbitrary real projects from the registry.

All per-project KB/overview/wiki/ask/graph/validate/docgen/okf data tests must use
sample_workspace (shop-federation + ledger-standalone). Real device projects are forbidden
as test data to keep the suite machine-agnostic and the public repo free of real paths.

This is a static source-code check — GPU-free, daemon-free, import-free.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_LIVE_DIR = Path(__file__).parent

# Files exempt from the list_projects() check — they use it for registry mechanics,
# not for picking arbitrary data projects.
_LIST_PROJECTS_ALLOWLIST = {
    "_projects.py",           # resolver module (hard-fails now, no registry fallback)
    "conftest.py",            # sample_workspace builder
    "test_p5_server.py",      # register/remove round-trip + G5 default-resolution
    "test_p6_daemon.py",      # registry filter mechanics (test_tg2_unknown_key)
    "test_index_validity.py", # registry precondition check (verifies sample paths are registered)
    "test_no_real_project_in_tests.py",  # this file
}

# Files exempt from the _OSE-as-data-arg check — they use _OSE/_OSE_SRC only for
# source-file reads (vendor/, source inspect, scripts/).
_OSE_DATA_ALLOWLIST = {
    "_sample_workspace.py",  # _REPO_ROOT for vendor source reads
    "test_browser.py",       # reads dashboard.html from repo
    "test_no_code_semantic_regex.py",  # scans opencode_search source tree
    "test_inference_lanes.py",         # reads scripts/*.py source
    "test_p20_capabilities.py",        # reads scripts/*.py source
    "test_okf.py",                     # _OSE_SRC for vendor/okf + sweeps.py source reads
    "test_docgen_hierarchy_e2e.py",    # _OSE_SRC for vendor/docgen + sweeps.py source reads
    "test_feature_proof.py",           # _OSE_SRC for quality.py inspect read (fp16)
    "test_p5_server.py",               # _OSE_SRC for mcp.py/ask.py/routes_chat.py source reads
    "test_no_real_project_in_tests.py",  # this file
}

# Pattern: _OSE or _OSE_SRC used as a project_path= / project= / project_paths=[…] argument
# on the same logical line as a daemon call (overview/search/ask/graph/wiki/validate/enrich/okf/docgen).
_DATA_ARG_RE = re.compile(
    r"_OSE\b.*(?:project_path|project_paths|project)\s*[=\[]"
    r"|(?:project_path|project_paths|project)\s*[=\[]\s*[^\n]*_OSE\b",
)


def _iter_py_files():
    return sorted(_LIVE_DIR.glob("*.py"))


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
