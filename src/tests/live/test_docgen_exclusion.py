"""Live e2e: docgen-output exclusion (marker-based, Fix 2) — no mocks. D1-D6."""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.live


def _make_generated_docs(base: Path) -> Path:
    docs = base / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    meta = docs / "_meta"
    meta.mkdir()
    (meta / "provenance.json").write_text("{}")
    (docs / "README.md").write_text("# hi\n")
    return docs


def _make_plain_docs(base: Path) -> Path:
    docs = base / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "guide.md").write_text("# Guide\n")
    return docs


class TestMarkerExclusion:
    def test_d1_generated_ignored_plain_not(self, tmp_path):
        """D1: generated docs/ → is_ignored_path True; plain docs/ → False."""
        from opencode_search.index.discover import is_ignored_path
        gen_root = tmp_path / "gen"
        gen_root.mkdir()
        gen_docs = _make_generated_docs(gen_root)
        assert is_ignored_path(gen_docs / "README.md", gen_root)

        plain_root = tmp_path / "plain"
        plain_root.mkdir()
        _make_plain_docs(plain_root)
        assert not is_ignored_path(plain_root / "docs" / "guide.md", plain_root)

    def test_d2_iter_files_skips_generated_yields_plain(self, tmp_path):
        """D2: iter_files skips generated docs/*.md, yields plain docs/*.md."""
        from opencode_search.index.discover import iter_files
        root = tmp_path / "project"
        root.mkdir()
        # generated
        _make_generated_docs(root)
        # plain docs: use a different name so they coexist
        (root / "notes").mkdir()
        (root / "notes" / "guide.md").write_text("# Notes\n")
        # a plain source file
        (root / "main.py").write_text("x=1\n")
        files = {str(f.relative_to(root)) for f in iter_files(root)}
        assert "main.py" in files, "main.py must be indexed"
        assert "notes/guide.md" in files, "plain notes/guide.md must be indexed"
        gen_docs_mds = {f for f in files if f.startswith("docs/")}
        assert not gen_docs_mds, f"generated docs/ must not be indexed: {gen_docs_mds}"

    def test_d3_docs_not_in_ignored_dirs(self):
        """D3: global IGNORED_DIRS must not contain 'docs' (over-reach removed)."""
        from opencode_search.core.config import IGNORED_DIRS
        assert "docs" not in IGNORED_DIRS

    def test_d4_discover_members_reaches_docs_named_dirs(self, tmp_path):
        """D4: discover_members walks through dirs named 'docs' (not blocked globally)."""
        from opencode_search.index.discover import iter_files
        root = tmp_path / "root"
        root.mkdir()
        plain_docs = root / "docs"
        plain_docs.mkdir()
        (plain_docs / "README.md").write_text("# Docs\n")
        files = {str(f.relative_to(root)) for f in iter_files(root)}
        assert "docs/README.md" in files, \
            "plain docs/ dir must be walkable for indexing"

    def test_d5_watcher_ignores_generated_docs_events(self, tmp_path):
        """D5: watcher is_ignored_path fires True for a file inside generated docs/, not plain."""
        from opencode_search.index.discover import is_ignored_path
        proj_root = tmp_path / "proj"
        proj_root.mkdir()
        gen_docs = _make_generated_docs(proj_root)
        # A real watcher event on a generated docs file → skipped
        assert is_ignored_path(gen_docs / "README.md", proj_root)
        # A file not under generated docs → not skipped
        (proj_root / "src").mkdir()
        (proj_root / "src" / "main.py").write_text("x=1\n")
        assert not is_ignored_path(proj_root / "src" / "main.py", proj_root)

    def test_d6_structure_skips_generated_docs(self, tmp_path):
        """D6: iter_files (used by kb/structure.py) skips generated docs subtree."""
        from opencode_search.index.discover import iter_files
        root = tmp_path / "svc"
        root.mkdir()
        _make_generated_docs(root)
        (root / "app.go").write_text("package main\n")
        files = {str(f.relative_to(root)) for f in iter_files(root)}
        assert "app.go" in files
        assert not any(f.startswith("docs/") for f in files), \
            f"generated docs/* must not appear: {[f for f in files if f.startswith('docs/')]}"
