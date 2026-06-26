"""Phase 2 — docgen kill-switch and no-output invariant across all indexed projects."""
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


def _should_skip(path: str) -> bool:
    p = Path(path)
    return (
        "ocs-test-dirs" in path
        or p.name.startswith("tmp")
        or p.name.startswith("test-")
    )


def _collect_projects() -> list[str]:
    from opencode_search.core.registry import list_projects
    return [
        p.path for p in list_projects()
        if p.enabled and not _should_skip(p.path) and Path(p.path).is_dir()
    ]


_ALL_PROJECTS = _collect_projects()
_PROJECT_IDS = [Path(pp).name for pp in _ALL_PROJECTS]


@pytest.mark.parametrize("project_path", _ALL_PROJECTS, ids=_PROJECT_IDS)
def test_kill_switch_produces_no_output(project_path, tmp_path):
    """OSE_DOCGEN=0 -> generate() returns mode=off and writes zero files."""
    prev = os.environ.get("OSE_DOCGEN")
    os.environ["OSE_DOCGEN"] = "0"
    try:
        r = generate(project_path=project_path, docs_dir=str(tmp_path))
    finally:
        if prev is None:
            os.environ.pop("OSE_DOCGEN", None)
        else:
            os.environ["OSE_DOCGEN"] = prev
    assert r.get("mode") == "off", f"{Path(project_path).name}: expected mode=off, got {r}"
    assert r["written"] == []
    assert not list(tmp_path.rglob("*.md")), f"{Path(project_path).name}: kill-switch produced files"


@pytest.mark.parametrize("project_path", _ALL_PROJECTS, ids=_PROJECT_IDS)
def test_no_home_path_leak_in_vendor(project_path):
    """No absolute home path may appear in vendor/docgen source files."""
    vendor_src = Path(__file__).parent.parent.parent.parent / "vendor" / "docgen" / "src"
    home = str(Path.home())
    for py in vendor_src.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        assert home not in text, f"Home path leaked in vendor/docgen/{py.relative_to(vendor_src)}"
    # Parameterized but only needs one project to run — skip duplicates
