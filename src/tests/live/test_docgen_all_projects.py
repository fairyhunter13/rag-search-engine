"""Phase 2 — docgen kill-switch and no-output invariant across sample projects."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_VENDOR_SRC = Path(__file__).parent.parent.parent.parent / "vendor" / "docgen" / "src"
if str(_VENDOR_SRC) not in sys.path:
    sys.path.insert(0, str(_VENDOR_SRC))

from ose_docgen.generate import generate  # noqa: E402

# Static committed fixture dirs (no indexing needed — kill-switch test only needs a real dir)
_FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "sample_projects"
_STATIC_DIRS = [
    _FIXTURE_ROOT / "shop-federation" / "cart-svc",
    _FIXTURE_ROOT / "shop-federation" / "checkout-svc",
    _FIXTURE_ROOT / "shop-federation" / "promo-svc",
    _FIXTURE_ROOT / "ledger-standalone",
]


@pytest.mark.parametrize("project_path", _STATIC_DIRS, ids=[d.name for d in _STATIC_DIRS])
def test_kill_switch_produces_no_output(project_path: Path, tmp_path):
    """OSE_DOCGEN=0 -> generate() returns mode=off and writes zero files."""
    prev = os.environ.get("OSE_DOCGEN")
    os.environ["OSE_DOCGEN"] = "0"
    try:
        r = generate(project_path=str(project_path), docs_dir=str(tmp_path))
    finally:
        if prev is None:
            os.environ.pop("OSE_DOCGEN", None)
        else:
            os.environ["OSE_DOCGEN"] = prev
    assert r.get("mode") == "off", f"{project_path.name}: expected mode=off, got {r}"
    assert r["written"] == []
    assert not list(tmp_path.rglob("*.md")), f"{project_path.name}: kill-switch produced files"


def test_no_home_path_leak_in_vendor():
    """No absolute home path may appear in vendor/docgen source files."""
    vendor_src = Path(__file__).parent.parent.parent.parent / "vendor" / "docgen" / "src"
    home = str(Path.home())
    for py in vendor_src.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        assert home not in text, f"Home path leaked in vendor/docgen/{py.relative_to(vendor_src)}"
