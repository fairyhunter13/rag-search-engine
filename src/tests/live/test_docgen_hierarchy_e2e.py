"""Phase 2 — docgen IH: kill-switch, source guards, MCP-absence, manual-trigger. (no mocks)"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.live._sample_workspace import SampleWorkspace

pytestmark = pytest.mark.live

_VENDOR_SRC = Path(__file__).parent.parent.parent.parent / "vendor" / "docgen" / "src"
if str(_VENDOR_SRC) not in sys.path:
    sys.path.insert(0, str(_VENDOR_SRC))

_OSE_SRC = Path(__file__).resolve().parents[3]  # source-file reads only


@pytest.fixture(scope="module")
def service_path(sample_workspace: SampleWorkspace) -> str:
    return sample_workspace.promo



def test_ih_kill_switch_off(tmp_path, service_path):
    """Phase 2: OSE_DOCGEN=0 -> generate() returns mode=off, writes nothing."""
    from ose_docgen.generate import generate
    prev = os.environ.get("OSE_DOCGEN")
    os.environ["OSE_DOCGEN"] = "0"
    try:
        r = generate(project_path=service_path, docs_dir=str(tmp_path))
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
    vendor_src = _OSE_SRC / "vendor" / "docgen" / "src"
    for py in vendor_src.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        assert "import tree_sitter" not in text and "from tree_sitter" not in text, \
            f"tree_sitter import found in {py.relative_to(vendor_src)}"


def test_ih_no_c4_skeleton_files():
    """Phase 2: C4 skeleton files deleted (tree.py, _tree_a/b/c.py)."""
    vendor_src = _OSE_SRC / "vendor" / "docgen" / "src" / "ose_docgen"
    for dead_file in ("tree.py", "_tree_a.py", "_tree_b.py", "_tree_c.py", "_tree_util.py"):
        assert not (vendor_src / dead_file).exists(), f"Dead C4 skeleton file still present: {dead_file}"


def test_ih_run_docgen_off_touches_nothing(service_path):
    """Phase 2: OSE_DOCGEN=0 -> run_docgen() is a no-op (touches no files)."""
    from rag_search.kb.docgen import run_docgen
    ih_dir = Path(service_path) / "docs" / "information-hierarchy"
    existed = ih_dir.exists()
    prev = os.environ.get("OSE_DOCGEN")
    os.environ["OSE_DOCGEN"] = "0"
    try:
        run_docgen(service_path)
    finally:
        if prev is None:
            os.environ.pop("OSE_DOCGEN", None)
        else:
            os.environ["OSE_DOCGEN"] = prev
    assert ih_dir.exists() == existed


def test_ih_not_in_auto_sweep(live_client):
    """Phase 2: /api/docgen is absent from MCP; MCP only has index/search/ask/graph/overview."""
    from rag_search.server.mcp import _MCP_TOOLS
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
    sweeps_path = _OSE_SRC / "src" / "rag_search" / "daemon" / "sweeps.py"
    src = sweeps_path.read_text()
    lines = src.splitlines()
    in_enrich = False
    for line in lines:
        if "def _enrich_project" in line:
            in_enrich = True
        if in_enrich and "run_docgen" in line and not line.strip().startswith("#"):
            pytest.fail(f"run_docgen still called in _enrich_project: {line.strip()}")


@pytest.mark.slow
def test_ih_generate_llm_structure(tmp_path, service_path, capfd):
    """Phase 2 (@slow): LLM-native IH generation produces valid IH structure."""
    from ose_docgen.generate import generate
    with capfd.disabled():
        r = generate(project_path=service_path, docs_dir=str(tmp_path / "docs"), max_pages=3)
    assert r.get("mode") != "off", "OSE_DOCGEN must not be 0 for this slow test"
    assert "no_profile" not in r.get("errors", []), "claude profile must be configured"
    ih_dir = tmp_path / "docs" / "information-hierarchy"
    assert ih_dir.exists(), "information-hierarchy/ dir must be created"
    pages = list(ih_dir.rglob("*.md"))
    assert pages, f"at least one IH page must be written; generate errors={r.get('errors', [])}"
    # index.md must appear in the architect's plan (write may fail due to API limits)
    import json as _json
    meta_plan = ih_dir / "_meta" / "ih_plan.json"
    assert meta_plan.exists(), "_meta/ih_plan.json must exist (architect succeeded)"
    plan_paths = {pg.get("path", "") for pg in _json.loads(meta_plan.read_text()).get("pages", [])}
    assert "index.md" in plan_paths, "architect plan must include index.md as IH spine"
    home = str(Path.home())
    for p in pages:
        assert home not in p.read_text(encoding="utf-8", errors="replace"), \
            f"home path leaked in {p.name}"
    assert not any("fragment_" in p.name for p in pages), "numeric-sequence naming found (must be semantic)"
