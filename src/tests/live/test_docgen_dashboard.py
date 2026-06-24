"""WS-D live e2e: /api/docs endpoints + run_docgen pipeline guards.

No mocks. Requires daemon at :8765. No GPU/LLM needed (OSE_DOCGEN_LLM=0).
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_VENDOR_SRC = Path(__file__).parent.parent.parent.parent / "vendor" / "docgen" / "src"
if str(_VENDOR_SRC) not in sys.path:
    sys.path.insert(0, str(_VENDOR_SRC))


def _any_project() -> str | None:
    from opencode_search.core.config import project_graph_db
    from opencode_search.core.registry import list_projects
    for p in list_projects():
        if not p.enabled:
            continue
        if "ocs-test-dirs" in p.path or Path(p.path).name.startswith(("tmp", "test-")):
            continue
        if project_graph_db(p.path).exists():
            return p.path
    return None


def _gen(proj: str, out: Path) -> None:
    from ose_docgen.generate import generate

    from opencode_search.core.config import project_graph_db
    generate(project_path=out, graph_db_path=project_graph_db(proj),
             docs_dir=str(out / "docs"), llm=False)


@pytest.fixture(scope="module")
def docs_proj_tmp(tmp_path_factory):
    proj = _any_project()
    if not proj:
        pytest.fail("no enabled project with graph.db — index a project first")
    out = tmp_path_factory.mktemp("docgen")
    _gen(proj, out)
    return out


class TestDocsApi:
    def test_tree_non_empty_and_c4_order(self, live_client, docs_proj_tmp) -> None:
        r = live_client.get(f"/api/docs?project={docs_proj_tmp}")
        assert r.status_code == 200
        tree = r.json().get("tree", [])
        assert len(tree) >= 5, f"expected ≥5 files, got {tree}"
        # README.md must precede 01-context entries if both exist
        readme_i = next((i for i, f in enumerate(tree) if f == "README.md"), None)
        ctx_i = next((i for i, f in enumerate(tree) if f.startswith("01-context")), None)
        if readme_i is not None and ctx_i is not None:
            assert readme_i < ctx_i

    def test_page_serves_heading_no_path_leak(self, live_client, docs_proj_tmp) -> None:
        tree = live_client.get(f"/api/docs?project={docs_proj_tmp}").json().get("tree", [])
        assert tree
        for rel in tree[:3]:
            r = live_client.get(f"/api/docs/page?project={docs_proj_tmp}&path={rel}")
            assert r.status_code == 200
            content = r.json().get("content", "")
            assert "# " in content, f"{rel}: no heading"
            assert "/home/" not in content, f"{rel}: path leaked"

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
        from opencode_search.index.discover import is_ignored_path
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

    def test_idempotent(self, docs_proj_tmp) -> None:
        """Second generate() with unchanged graph.db must not change any file."""
        proj = _any_project()
        if not proj:
            pytest.fail("no enabled project — index a project first")
        docs = docs_proj_tmp / "docs"
        h1 = {str(f.relative_to(docs)): hashlib.sha256(f.read_bytes()).hexdigest()
               for f in docs.rglob("*.md")}
        _gen(proj, docs_proj_tmp)
        h2 = {str(f.relative_to(docs)): hashlib.sha256(f.read_bytes()).hexdigest()
               for f in docs.rglob("*.md")}
        assert h1 == h2, "second run changed files"

    def test_no_errors(self, docs_proj_tmp) -> None:
        """generate() must return no errors."""
        from ose_docgen.generate import generate

        from opencode_search.core.config import project_graph_db
        proj = _any_project()
        if not proj:
            pytest.fail("no enabled project — index a project first")
        out2 = Path(str(docs_proj_tmp) + "-errors-check")
        out2.mkdir(exist_ok=True)
        r = generate(project_path=out2, graph_db_path=project_graph_db(proj),
                     docs_dir=str(out2 / "docs"), llm=False)
        assert r.get("errors", []) == [], f"errors: {r['errors']}"
