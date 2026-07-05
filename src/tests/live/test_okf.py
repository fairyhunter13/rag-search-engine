"""Phase 2c — OKF v0.1: kill-switch, source guards, MCP-absence, manual-trigger. (no mocks)"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_VENDOR = Path(__file__).resolve().parents[3] / "vendor" / "okf" / "src"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

_OSE_SRC = Path(__file__).resolve().parents[3]  # source-file reads only

from tests.live._sample_workspace import SampleWorkspace


@pytest.fixture(scope="module")
def service_path(sample_workspace: SampleWorkspace) -> str:
    return sample_workspace.promo


def test_okf_version_constant():
    from okf.generate import OKF_VERSION
    assert OKF_VERSION == "0.1"


def test_okf_kill_switch_off(tmp_path, service_path):
    """OSE_OKF=0 -> generate() returns mode=off, writes nothing."""
    from okf.generate import generate
    prev = os.environ.get("OSE_OKF")
    os.environ["OSE_OKF"] = "0"
    try:
        r = generate(project_path=service_path, out_dir=str(tmp_path))
    finally:
        if prev is None:
            os.environ.pop("OSE_OKF", None)
        else:
            os.environ["OSE_OKF"] = prev
    assert r.get("mode") == "off", f"Expected mode=off, got {r}"
    assert r["written"] == []
    assert not list(tmp_path.rglob("*.md")), "kill-switch must produce no files"


def test_okf_adapter_kill_switch_returns_dict(service_path):
    """run_okf with OSE_OKF=0 returns dict with mode=off."""
    from rag_search.kb.okf import run_okf
    prev = os.environ.get("OSE_OKF")
    os.environ["OSE_OKF"] = "0"
    try:
        r = run_okf(service_path)
    finally:
        if prev is None:
            os.environ.pop("OSE_OKF", None)
        else:
            os.environ["OSE_OKF"] = prev
    assert isinstance(r, dict), f"run_okf must return dict, got {type(r)}"
    assert r.get("mode") == "off"
    assert r["written"] == []


def test_okf_no_tree_sitter_import_in_vendor():
    """No 'import tree_sitter' on the OKF doc-tooling path."""
    for py in _VENDOR.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        assert "import tree_sitter" not in text and "from tree_sitter" not in text, \
            f"tree_sitter import found in vendor/okf/{py.relative_to(_VENDOR)}"


def test_okf_no_fragment_naming_in_vendor():
    """OKF generates semantic concept names, never fragment_N.md."""
    src = _OSE_SRC / "vendor" / "okf" / "src"
    for py in src.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        assert "fragment_" not in text, \
            f"fragment_ naming found in vendor/okf/{py.relative_to(src)}"


def test_okf_not_in_mcp_tools(live_client):
    """OKF must NOT be an MCP tool; MCP = index/search/ask/graph/overview only."""
    from rag_search.server.mcp import _MCP_TOOLS
    tool_names = {t.name for t in _MCP_TOOLS}
    assert "okf" not in tool_names, "okf must NOT be an MCP tool"
    assert "docgen" not in tool_names, "docgen must NOT be an MCP tool"


def test_okf_api_route_present(live_client):
    """/api/okf POST route exists and requires project_path."""
    r = live_client.post("/api/okf", json={})
    assert r.status_code == 400, f"/api/okf without project must return 400: {r.status_code}"


def test_okf_api_route_no_project_field(live_client):
    """/api/okf POST with JSON body missing project_path returns 400."""
    r = live_client.post("/api/okf", json={"other": "field"})
    assert r.status_code == 400, f"/api/okf missing project_path must return 400: {r.status_code}"


def test_okf_not_in_sweeps():
    """run_okf is not called from _enrich_project in sweeps.py."""
    sweeps_path = _OSE_SRC / "src" / "rag_search" / "daemon" / "sweeps.py"
    src = sweeps_path.read_text()
    lines = src.splitlines()
    in_enrich = False
    for line in lines:
        if "def _enrich_project" in line:
            in_enrich = True
        if in_enrich and "run_okf" in line and not line.strip().startswith("#"):
            pytest.fail(f"run_okf still called in _enrich_project: {line.strip()}")


def test_okf_no_home_path_in_vendor():
    """No absolute home path may appear in vendor/okf source files."""
    home = str(Path.home())
    for py in _VENDOR.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        assert home not in text, f"Home path leaked in vendor/okf/{py.relative_to(_VENDOR)}"


def test_okf_index_md_no_frontmatter_in_source():
    """P9 spec: index.md assembly must NOT call _frontmatter (OKF v0.1: reserved files carry no frontmatter)."""
    gen_py = _VENDOR / "okf" / "generate.py"
    src = gen_py.read_text(encoding="utf-8")
    lines = src.splitlines()
    in_index_section = False
    for line in lines:
        if "# index.md" in line:
            in_index_section = True
        elif in_index_section and line.startswith("    # ") and "index.md" not in line:
            break
        elif in_index_section and "_frontmatter" in line:
            raise AssertionError(
                f"P9: index.md assembly calls _frontmatter — OKF v0.1 spec violation: {line.strip()}"
            )


@pytest.mark.slow
def test_okf_llm_generate_structure(tmp_path, service_path, capfd):
    """Phase 2c (@slow): LLM-native OKF generates valid v0.1 bundle structure."""
    from okf.generate import OKF_VERSION, generate
    with capfd.disabled():
        r = generate(project_path=service_path, out_dir=str(tmp_path / "okf"))
    assert r.get("mode") != "off", "OSE_OKF must not be 0 for this slow test"
    assert "no_profile" not in r.get("errors", []), "claude profile must be configured"
    assert "discover_failed" not in r.get("errors", []), f"OKF discover failed: {r.get('errors')} debug={r.get('_debug')}"
    out = tmp_path / "okf"
    assert out.exists(), "OKF output dir must be created"
    pages = list(out.rglob("*.md"))
    assert pages, "at least one OKF page must be written"
    index = out / "index.md"
    assert index.exists(), "index.md must be present"
    # OKF v0.1 spec: index.md and log.md must carry NO frontmatter
    index_text = index.read_text(encoding="utf-8")
    assert not index_text.startswith("---"), "P9: index.md must not have frontmatter (OKF v0.1 spec)"
    # Concept pages: frontmatter required
    concept_pages = [p for p in pages if p.name not in ("index.md", "log.md")]
    for p in concept_pages:
        text = p.read_text(encoding="utf-8", errors="replace")
        assert f'okf_version: "{OKF_VERSION}"' in text, f"{p.name}: missing okf_version"
        assert "type:" in text, f"{p.name}: missing required type field"
        assert str(Path.home()) not in text, f"home path leaked in {p.name}"
        assert "fragment_" not in p.name, f"fragment_ naming found: {p.name}"
