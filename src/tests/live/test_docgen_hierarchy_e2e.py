"""Phase 2 — docgen IH: kill-switch, source guards, MCP-absence, manual-trigger. (no mocks)"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_VENDOR_SRC = Path(__file__).parent.parent.parent.parent / "vendor" / "docgen" / "src"
if str(_VENDOR_SRC) not in sys.path:
    sys.path.insert(0, str(_VENDOR_SRC))

_OSE = str(Path(__file__).resolve().parents[3])


def _any_project():
    from opencode_search.core.registry import list_projects
    for p in list_projects():
        if not p.enabled or "ocs-test-dirs" in p.path:
            continue
        if Path(p.path).name.startswith(("tmp", "test-")):
            continue
        if Path(p.path).is_dir():
            return p.path
    return None



def test_ih_kill_switch_off(tmp_path):
    """Phase 2: OSE_DOCGEN=0 -> generate() returns mode=off, writes nothing."""
    from ose_docgen.generate import generate
    proj = _any_project()
    assert proj, "no enabled project"
    prev = os.environ.get("OSE_DOCGEN")
    os.environ["OSE_DOCGEN"] = "0"
    try:
        r = generate(project_path=proj, docs_dir=str(tmp_path))
    finally:
        if prev is None:
            os.environ.pop("OSE_DOCGEN", None)
        else:
            os.environ["OSE_DOCGEN"] = prev
    assert r.get("mode") == "off"
    assert r["written"] == []
    assert not list(tmp_path.rglob("*.md")), "kill-switch must produce no files"


def test_ih_no_tree_sitter_import_in_vendor():
    """Phase 2: no 'import tree_sitter' on the doc-tooling path (vendor/docgen)."""
    vendor_src = Path(_OSE) / "vendor" / "docgen" / "src"
    for py in vendor_src.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        assert "import tree_sitter" not in text and "from tree_sitter" not in text, \
            f"tree_sitter import found in {py.relative_to(vendor_src)}"


def test_ih_no_c4_skeleton_files():
    """Phase 2: C4 skeleton files deleted (tree.py, _tree_a/b/c.py)."""
    vendor_src = Path(_OSE) / "vendor" / "docgen" / "src" / "ose_docgen"
    for dead_file in ("tree.py", "_tree_a.py", "_tree_b.py", "_tree_c.py", "_tree_util.py"):
        assert not (vendor_src / dead_file).exists(), f"Dead C4 skeleton file still present: {dead_file}"


def test_ih_run_docgen_off_touches_nothing():
    """Phase 2: OSE_DOCGEN=0 -> run_docgen() is a no-op (touches no files)."""
    from opencode_search.kb.docgen import run_docgen
    proj = _any_project()
    assert proj
    ih_dir = Path(proj) / "docs" / "information-hierarchy"
    existed = ih_dir.exists()
    prev = os.environ.get("OSE_DOCGEN")
    os.environ["OSE_DOCGEN"] = "0"
    try:
        run_docgen(proj)
    finally:
        if prev is None:
            os.environ.pop("OSE_DOCGEN", None)
        else:
            os.environ["OSE_DOCGEN"] = prev
    assert ih_dir.exists() == existed


def test_ih_not_in_auto_sweep(live_client):
    """Phase 2: /api/docgen is absent from MCP; MCP only has index/search/ask/graph/overview."""
    from opencode_search.server.mcp import _MCP_TOOLS
    tool_names = {t.name for t in _MCP_TOOLS}
    assert "docgen" not in tool_names, "docgen must NOT be an MCP tool"
    assert "okf" not in tool_names, "okf must NOT be an MCP tool"


def test_ih_api_docgen_route_present(live_client):
    """Phase 2: /api/docgen POST route exists and requires project param."""
    r = live_client.post("/api/docgen", json={})
    assert r.status_code == 400, f"/api/docgen without project must return 400: {r.status_code}"


def test_ih_api_docgen_route_no_project_field(live_client):
    """Phase 2: /api/docgen with JSON body missing project_path returns 400."""
    r = live_client.post("/api/docgen", json={"other": "field"})
    assert r.status_code == 400, f"/api/docgen missing project_path must return 400: {r.status_code}"


def test_ih_docgen_not_in_sweeps():
    """Phase 2: run_docgen is not called from _enrich_project in sweeps.py."""
    sweeps_path = Path(_OSE) / "src" / "opencode_search" / "daemon" / "sweeps.py"
    src = sweeps_path.read_text()
    lines = src.splitlines()
    in_enrich = False
    for line in lines:
        if "def _enrich_project" in line:
            in_enrich = True
        if in_enrich and "run_docgen" in line and not line.strip().startswith("#"):
            pytest.fail(f"run_docgen still called in _enrich_project: {line.strip()}")


@pytest.mark.slow
def test_ih_generate_llm_structure(tmp_path):
    """Phase 2 (@slow): LLM-native IH generation produces valid IH structure."""
    from ose_docgen.generate import generate
    proj = _any_project()
    assert proj, "no enabled project found"
    r = generate(project_path=proj, docs_dir=str(tmp_path / "docs"))
    assert r.get("mode") != "off", "OSE_DOCGEN must not be 0 for this slow test"
    assert "no_profile" not in r.get("errors", []), "claude profile must be configured"
    ih_dir = tmp_path / "docs" / "information-hierarchy"
    assert ih_dir.exists(), "information-hierarchy/ dir must be created"
    pages = list(ih_dir.rglob("*.md"))
    assert pages, "at least one IH page must be written"
    assert (ih_dir / "index.md").exists(), "index.md must be present"
    home = str(Path.home())
    for p in pages:
        assert home not in p.read_text(encoding="utf-8", errors="replace"), \
            f"home path leaked in {p.name}"
    assert not any("fragment_" in p.name for p in pages), "numeric-sequence naming found (must be semantic)"
