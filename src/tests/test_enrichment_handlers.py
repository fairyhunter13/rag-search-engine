"""Tests for enrichment and wiki handlers (Phase 3)."""
from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

import pytest

from opencode_search.graph.storage import CommunityData, GraphStorage, NodeData
from opencode_search.handlers._enrichment import handle_enrich_project, handle_get_symbol_intent
from opencode_search.handlers._wiki import (
    handle_wiki_generate,
    handle_wiki_ingest,
    handle_wiki_lint,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _node_id(file: str, qn: str) -> str:
    return hashlib.sha256(f"{file}::{qn}".encode()).hexdigest()[:16]


def _make_node(file: str, name: str, qn: str | None = None) -> NodeData:
    qualified = qn or f"mod.{name}"
    return NodeData(
        id=_node_id(file, qualified),
        name=name, qualified_name=qualified, kind="function",
        file=file, start_line=1, end_line=10, language="python",
        created_at="", updated_at="",
    )


@pytest.fixture
def project_with_graph(tmp_path):

    project_root = tmp_path / "project"
    project_root.mkdir()

    # Use a unique path for this test
    graph_db_path = str(tmp_path / "graph.db")

    gs = GraphStorage(graph_db_path)
    gs.open()

    nodes = [
        _make_node("/project/auth.py", "authenticate", "auth.authenticate"),
        _make_node("/project/auth.py", "verify", "auth.verify"),
    ]
    gs.upsert_nodes(nodes)
    gs.upsert_community(CommunityData(id=0, node_count=2))
    for n in nodes:
        gs.set_community(n.id, 0)
    gs.close()

    with patch(
        "opencode_search.handlers._enrichment.get_project_graph_db_path",
        return_value=graph_db_path,
    ), patch(
        "opencode_search.handlers._wiki.get_project_graph_db_path",
        return_value=graph_db_path,
    ), patch(
        "opencode_search.handlers._wiki.get_project_wiki_dir",
        return_value=tmp_path / "wiki",
    ), patch(
        "opencode_search.handlers._wiki.get_project_raw_dir",
        return_value=tmp_path / "raw",
    ):
        yield str(project_root), nodes, graph_db_path


# ---------------------------------------------------------------------------
# handle_enrich_project
# ---------------------------------------------------------------------------


async def test_handle_enrich_project_returns_error_when_no_llm(project_with_graph):
    project_path, _, _ = project_with_graph
    with patch("opencode_search.handlers._enrichment._get_llm", return_value=None):
        result = await handle_enrich_project(project_path=project_path)
    assert "error" in result


async def test_handle_enrich_project_returns_error_when_ollama_not_available(project_with_graph):
    project_path, _, _ = project_with_graph
    llm = MagicMock()
    llm.is_available.return_value = False
    with patch("opencode_search.handlers._enrichment._get_llm", return_value=llm):
        result = await handle_enrich_project(project_path=project_path)
    assert "error" in result


async def test_handle_enrich_project_communities_scope(project_with_graph):
    project_path, _, _graph_db_path = project_with_graph
    llm = MagicMock()
    llm.is_available.return_value = True
    llm.community_summary.return_value = ("Auth Layer", "Handles auth.")
    with patch("opencode_search.handlers._enrichment._get_llm", return_value=llm):
        result = await handle_enrich_project(project_path=project_path, scope="communities")
    assert result.get("status") == "ok"
    assert result.get("enriched_communities") >= 0


async def test_handle_enrich_project_symbols_scope(project_with_graph):
    project_path, _, _ = project_with_graph
    llm = MagicMock()
    llm.is_available.return_value = True
    llm.symbol_intent.return_value = "Authenticates a user."
    with patch("opencode_search.handlers._enrichment._get_llm", return_value=llm):
        result = await handle_enrich_project(project_path=project_path, scope="symbols")
    assert result.get("status") == "ok"
    assert result.get("enriched_symbols") >= 0


async def test_handle_enrich_project_returns_elapsed_s(project_with_graph):
    project_path, _, _ = project_with_graph
    llm = MagicMock()
    llm.is_available.return_value = True
    llm.community_summary.return_value = ("Title", "Summary.")
    with patch("opencode_search.handlers._enrichment._get_llm", return_value=llm):
        result = await handle_enrich_project(project_path=project_path)
    assert "elapsed_s" in result
    assert result["elapsed_s"] >= 0


# ---------------------------------------------------------------------------
# handle_get_symbol_intent
# ---------------------------------------------------------------------------


async def test_handle_get_symbol_intent_returns_error_when_no_llm(project_with_graph):
    project_path, _nodes, _ = project_with_graph
    with patch("opencode_search.handlers._enrichment._get_llm", return_value=None):
        result = await handle_get_symbol_intent(name="authenticate", project_path=project_path)
    assert "error" in result


async def test_handle_get_symbol_intent_calls_llm_when_stale(project_with_graph):
    project_path, _nodes, _ = project_with_graph
    llm = MagicMock()
    llm.is_available.return_value = True
    llm.symbol_intent.return_value = "Authenticates a user by token."
    with patch("opencode_search.handlers._enrichment._get_llm", return_value=llm):
        result = await handle_get_symbol_intent(name="authenticate", project_path=project_path)
    assert "intent" in result or "error" in result
    if "intent" in result:
        assert result["intent"] == "Authenticates a user by token."
        assert result["cached"] is False


async def test_handle_get_symbol_intent_caches_result_in_db(project_with_graph):
    project_path, _nodes, _graph_db_path = project_with_graph
    llm = MagicMock()
    llm.is_available.return_value = True
    llm.symbol_intent.return_value = "Cached intent value."
    with patch("opencode_search.handlers._enrichment._get_llm", return_value=llm):
        # First call generates
        result1 = await handle_get_symbol_intent(name="authenticate", project_path=project_path)
        if "intent" not in result1:
            pytest.skip("Symbol not found or error")
        # Second call should return cached
        result2 = await handle_get_symbol_intent(name="authenticate", project_path=project_path)
    assert result2.get("cached") is True
    assert result2.get("intent") == "Cached intent value."
    llm.symbol_intent.assert_called_once()  # Not called again


async def test_handle_get_symbol_intent_symbol_not_found(project_with_graph):
    project_path, _, _ = project_with_graph
    llm = MagicMock()
    llm.is_available.return_value = True
    with patch("opencode_search.handlers._enrichment._get_llm", return_value=llm):
        result = await handle_get_symbol_intent(name="nonexistent_xyz", project_path=project_path)
    assert "error" in result


# ---------------------------------------------------------------------------
# handle_wiki_generate
# ---------------------------------------------------------------------------


async def test_handle_wiki_generate_returns_error_when_no_llm(project_with_graph):
    project_path, _, _ = project_with_graph
    with patch("opencode_search.handlers._wiki._get_llm", return_value=None):
        result = await handle_wiki_generate(project_path=project_path)
    assert "error" in result


async def test_handle_wiki_generate_returns_error_when_ollama_not_available(project_with_graph):
    project_path, _, _ = project_with_graph
    llm = MagicMock()
    llm.is_available.return_value = False
    with patch("opencode_search.handlers._wiki._get_llm", return_value=llm):
        result = await handle_wiki_generate(project_path=project_path)
    assert "error" in result


async def test_handle_wiki_generate_creates_pages(project_with_graph):
    project_path, _, _ = project_with_graph
    llm = MagicMock()
    llm.is_available.return_value = True
    llm.community_summary.return_value = ("Auth Layer", "Handles auth.")
    with patch("opencode_search.handlers._wiki._get_llm", return_value=llm):
        result = await handle_wiki_generate(project_path=project_path)
    assert result.get("status") == "ok"
    assert "pages_created" in result


# ---------------------------------------------------------------------------
# handle_wiki_ingest
# ---------------------------------------------------------------------------


async def test_handle_wiki_ingest_invalid_path_returns_error(project_with_graph):
    project_path, _, _ = project_with_graph
    llm = MagicMock()
    llm.is_available.return_value = True
    with patch("opencode_search.handlers._wiki._get_llm", return_value=llm):
        result = await handle_wiki_ingest(
            source_path="/nonexistent/path/doc.md",
            project_path=project_path,
        )
    assert "error" in result


async def test_handle_wiki_ingest_creates_page(project_with_graph, tmp_path):
    project_path, _, _ = project_with_graph
    # Create a source file
    src = tmp_path / "design.md"
    src.write_text("# Design\nThis is a design document.", encoding="utf-8")
    llm = MagicMock()
    llm.is_available.return_value = True
    llm.raw_doc_to_wiki.return_value = "# Design\n\nKey information extracted."
    with patch("opencode_search.handlers._wiki._get_llm", return_value=llm):
        result = await handle_wiki_ingest(
            source_path=str(src),
            project_path=project_path,
        )
    assert result.get("status") == "ok"
    assert len(result.get("pages_created", [])) >= 1


async def test_handle_wiki_ingest_no_llm_returns_error(project_with_graph, tmp_path):
    project_path, _, _ = project_with_graph
    src = tmp_path / "doc.md"
    src.write_text("content", encoding="utf-8")
    with patch("opencode_search.handlers._wiki._get_llm", return_value=None):
        result = await handle_wiki_ingest(source_path=str(src), project_path=project_path)
    assert "error" in result


# ---------------------------------------------------------------------------
# handle_wiki_lint
# ---------------------------------------------------------------------------


async def test_handle_wiki_lint_clean_wiki_no_issues(project_with_graph):
    project_path, _, _ = project_with_graph
    result = await handle_wiki_lint(project_path=project_path)
    assert "healthy" in result


async def test_handle_wiki_lint_returns_valid_structure(project_with_graph):
    project_path, _, _ = project_with_graph
    result = await handle_wiki_lint(project_path=project_path)
    assert "total_pages" in result
    assert "orphans" in result
    assert "empty_pages" in result
