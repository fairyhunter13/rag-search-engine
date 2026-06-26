"""WS-C: OKF v0.1 standalone generator live test.

GPU-free, OSE-daemon-free for Phase 1 cases (no inject needed).
Uses real vendor/okf/src directly via sys.path injection.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_VENDOR = Path(__file__).resolve().parents[3] / "vendor" / "okf" / "src"
sys.path.insert(0, str(_VENDOR))

from okf.generate import OKF_VERSION, generate

pytestmark = pytest.mark.live

_OSE = str(Path(__file__).resolve().parents[3])


class TestOKFStandalone:
    def test_okf_version_constant(self):
        assert OKF_VERSION == "0.1"

    def test_okf_generates_on_ose(self, tmp_path):
        result = generate(_OSE, out_dir=tmp_path / "okf")
        assert result["version"] == OKF_VERSION
        assert result["project"] == "opencode-search-engine"
        assert len(result["written"]) > 0
        assert result["errors"] == [] if "errors" in result else True

    def test_okf_index_has_frontmatter(self, tmp_path):
        out = tmp_path / "okf"
        generate(_OSE, out_dir=out)
        index = out / "index.md"
        assert index.exists()
        text = index.read_text()
        assert f'okf_version: "{OKF_VERSION}"' in text
        assert "generated: true" in text

    def test_okf_fragments_have_type_field(self, tmp_path):
        out = tmp_path / "okf"
        generate(_OSE, out_dir=out)
        for frag in out.glob("fragment_*.md"):
            content = frag.read_text()
            assert "type:" in content, f"fragment {frag.name} missing type field"

    def test_okf_no_absolute_paths(self, tmp_path):
        out = tmp_path / "okf"
        generate(_OSE, out_dir=out)
        home = str(Path.home())
        for md in out.glob("*.md"):
            assert home not in md.read_text(), f"Absolute path in {md.name}"

    def test_okf_adapter_no_raise(self, tmp_path):
        """kb.okf.run_okf must not raise."""
        from opencode_search.kb.okf import run_okf
        run_okf(_OSE)

    def test_okf_kill_switch(self, tmp_path):
        """OSE_OKF=0 skips silently."""
        import os
        os.environ["OSE_OKF"] = "0"
        try:
            from opencode_search.kb.okf import run_okf
            run_okf(_OSE)
        finally:
            del os.environ["OSE_OKF"]
