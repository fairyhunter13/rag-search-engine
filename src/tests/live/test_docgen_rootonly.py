"""Phase 2 — root-only docgen gate (GT1, GT2). No GPU/LLM needed.

GT1: _is_federation_member truth table + run_docgen on a member cleans, not generates.
GT2: cleanup_member_docs() cleans member docs/, leaves root docs/ intact.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_VENDOR_SRC = Path(__file__).parent.parent.parent.parent / "vendor" / "docgen" / "src"
if str(_VENDOR_SRC) not in sys.path:
    sys.path.insert(0, str(_VENDOR_SRC))

_GEN_FM = (
    "---\ngenerated: true\nsource_sig: abc123\n"
    "hier_version: fg1+lp2\nc4_level: context\n---\n\n"
)


def _seed_docs(docs: Path, *, with_human: bool = False) -> None:
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "_meta").mkdir()
    (docs / "_meta" / "provenance.json").write_text("{}")
    (docs / "README.md").write_text(_GEN_FM + "# Generated\n")
    if with_human:
        (docs / "MANUAL.md").write_text("# Human\n\nThis is hand-written.\n")


def _clean_reg(paths: list[str]) -> None:
    from opencode_search.core.registry import remove_project
    for p in paths:
        remove_project(p)


class TestFederationMemberGate:
    def test_gt1a_is_federation_member_truth_table(self, safe_tmp_path):
        """GT1a: _is_federation_member True for members, False for roots and strangers."""
        from opencode_search.core.config import ProjectEntry
        from opencode_search.core.registry import upsert_project
        from opencode_search.kb.docgen import _is_federation_member

        root = str(safe_tmp_path / "root")
        member = str(safe_tmp_path / "member")
        Path(root).mkdir()
        Path(member).mkdir()
        upsert_project(ProjectEntry(path=root, enabled=True, federation=[member]))
        upsert_project(ProjectEntry(path=member, enabled=True))
        try:
            assert _is_federation_member(member)
            assert not _is_federation_member(root)
            assert not _is_federation_member(str(safe_tmp_path / "stranger"))
        finally:
            _clean_reg([root, member])

    def test_gt1b_run_docgen_member_cleans_not_generates(self, safe_tmp_path):
        """GT1b: run_docgen on a federation member cleans docs/ instead of generating."""
        from opencode_search.core.config import ProjectEntry
        from opencode_search.core.registry import upsert_project
        from opencode_search.kb.docgen import run_docgen

        root = str(safe_tmp_path / "root2")
        member = str(safe_tmp_path / "member2")
        Path(root).mkdir()
        Path(member).mkdir()

        docs = Path(member) / "docs"
        _seed_docs(docs, with_human=True)
        human_bytes = (docs / "MANUAL.md").read_bytes()

        upsert_project(ProjectEntry(path=root, enabled=True, federation=[member]))
        upsert_project(ProjectEntry(path=member, enabled=True))
        try:
            run_docgen(member)
            assert not (docs / "README.md").exists(), "generated file must be removed"
            assert not (docs / "_meta" / "provenance.json").exists(), "_meta/ must be removed"
            assert (docs / "MANUAL.md").read_bytes() == human_bytes, "human file must be byte-identical"
        finally:
            _clean_reg([root, member])


def test_gt2_cleanup_member_docs_synthetic(safe_tmp_path):
    """GT2: cleanup_member_docs() cleans member docs/, leaves root plain docs/ intact."""
    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.registry import upsert_project
    from opencode_search.kb.docgen import cleanup_member_docs

    root = str(safe_tmp_path / "root3")
    member = str(safe_tmp_path / "member3")
    Path(root).mkdir()
    Path(member).mkdir()

    mdocs = Path(member) / "docs"
    _seed_docs(mdocs, with_human=True)
    human_bytes = (mdocs / "MANUAL.md").read_bytes()

    # Root: plain docs/ (no provenance.json — must survive)
    rdocs = Path(root) / "docs"
    rdocs.mkdir()
    root_page = rdocs / "ROOT.md"
    root_page.write_text("# Root\n")

    upsert_project(ProjectEntry(path=root, enabled=True, federation=[member]))
    upsert_project(ProjectEntry(path=member, enabled=True))
    try:
        report = cleanup_member_docs()
        hits = [r for r in report["members"] if r["path"] == member]
        assert hits, f"member not in report: {report}"
        assert hits[0]["removed"] > 0
        assert not (mdocs / "README.md").exists(), "generated README.md must be removed"
        assert (mdocs / "MANUAL.md").read_bytes() == human_bytes, "human file must survive"
        assert root_page.exists(), "root docs/ must not be touched"
    finally:
        _clean_reg([root, member])
        shutil.rmtree(rdocs, ignore_errors=True)
