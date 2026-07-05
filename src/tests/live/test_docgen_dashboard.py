"""WS-D live e2e: /api/docs endpoints + /api/docgen manual-trigger route. (no mocks)"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_VENDOR_SRC = Path(__file__).parent.parent.parent.parent / "vendor" / "docgen" / "src"
if str(_VENDOR_SRC) not in sys.path:
    sys.path.insert(0, str(_VENDOR_SRC))


class TestDocsApi:
    def test_traversal_blocked(self, live_client, tmp_path) -> None:
        (tmp_path / "docs").mkdir()
        bad_paths = [
            "../../etc/passwd",
            "../README.md",
            "/etc/hosts",
            "%2e%2e%2fetc%2fpasswd",   # URL-encoded ../
            "..%2Fetc%2Fpasswd",
        ]
        for bad in bad_paths:
            r = live_client.get(f"/api/docs/page?project={tmp_path}&path={bad}")
            assert r.status_code in (400, 404), f"traversal not blocked: {bad!r}"

    def test_empty_when_no_docs(self, live_client, tmp_path) -> None:
        r = live_client.get(f"/api/docs?project={tmp_path}")
        assert r.status_code == 200
        assert r.json().get("tree") == []

    def test_missing_page_404(self, live_client, tmp_path) -> None:
        (tmp_path / "docs").mkdir()
        r = live_client.get(f"/api/docs/page?project={tmp_path}&path=nope.md")
        assert r.status_code == 404


class TestDocgenPipeline:
    def test_docs_ignored_by_watcher(self, tmp_path) -> None:
        from rag_search.index.discover import is_ignored_path
        # generated docs tree (has provenance.json) → ignored
        gen = tmp_path / "repo" / "docs"
        gen.mkdir(parents=True)
        (gen / "_meta").mkdir()
        (gen / "_meta" / "provenance.json").write_text("{}")
        (gen / "README.md").write_text("# hi\n")
        root = tmp_path / "repo"
        assert is_ignored_path(gen / "README.md", root)
        # plain docs (no marker) → NOT ignored
        plain = tmp_path / "plain" / "docs"
        plain.mkdir(parents=True)
        (plain / "guide.md").write_text("# Guide\n")
        root2 = tmp_path / "plain"
        assert not is_ignored_path(plain / "guide.md", root2)

    def test_api_docgen_route_requires_project(self, live_client) -> None:
        """POST /api/docgen without project_path must return 400."""
        r = live_client.post("/api/docgen", json={})
        assert r.status_code == 400

    def test_api_docgen_no_project_field(self, live_client) -> None:
        """POST /api/docgen with JSON body missing project_path must return 400."""
        r = live_client.post("/api/docgen", json={"other": "field"})
        assert r.status_code == 400, f"missing project_path must return 400: {r.status_code}"

    def test_api_docgen_not_in_mcp(self, live_client) -> None:
        """docgen and okf must NOT appear as MCP tools."""
        from rag_search.server.mcp import _MCP_TOOLS
        tool_names = {t.name for t in _MCP_TOOLS}
        assert "docgen" not in tool_names
        assert "okf" not in tool_names
