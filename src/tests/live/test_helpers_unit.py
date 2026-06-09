"""Direct-import isolation tests for private helpers.

Tests each helper against real indexed data (quality_project = this repo).
No mocks — all calls hit the real graph DB, vector index, or Ollama.

Fixtures used:
  quality_project — opencode-search-engine path (enriched communities, real graph)
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_REPO = Path("/home/user/git/github.com/fairyhunter13/opencode-search-engine")


# ---------------------------------------------------------------------------
# _count_communities
# ---------------------------------------------------------------------------

class TestCountCommunities:
    """_count_communities must read graph.db reliably and fail gracefully."""

    def test_returns_int_for_indexed_project(self, quality_project):
        from opencode_search.handlers._query import _count_communities
        result = _count_communities(quality_project)
        assert isinstance(result, int), f"expected int, got {type(result)}"
        assert result >= 0

    def test_returns_zero_for_nonexistent_path(self):
        from opencode_search.handlers._query import _count_communities
        result = _count_communities("/tmp/no-such-project-xyzqrs")
        assert result == 0, f"expected 0 for nonexistent path, got {result}"

    def test_matches_direct_sql_count(self, quality_project):
        from opencode_search.config import get_project_graph_db_path
        from opencode_search.handlers._query import _count_communities
        db_path = get_project_graph_db_path(quality_project)
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            (expected,) = conn.execute("SELECT COUNT(*) FROM communities").fetchone()
        finally:
            conn.close()
        result = _count_communities(quality_project)
        assert result == expected, (
            f"_count_communities={result} but direct SQL={expected}"
        )


# ---------------------------------------------------------------------------
# _kb_chat.py private helpers
# ---------------------------------------------------------------------------

class TestKBChatHelpers:
    """_kb_chat helpers must return correct tuple shapes and degrade gracefully."""

    def test_fetch_code_context_returns_tuple(self, quality_project):
        from opencode_search.handlers._kb_chat import _fetch_code_context
        ctx, paths, count = asyncio.run(
            _fetch_code_context("search handler function", quality_project, top_k=5)
        )
        assert isinstance(ctx, str)
        assert isinstance(paths, list)
        assert isinstance(count, int)
        assert count >= 0

    def test_fetch_code_context_handles_nonsense_query_gracefully(self, quality_project):
        from opencode_search.handlers._kb_chat import _fetch_code_context
        ctx, paths, count = asyncio.run(
            _fetch_code_context("xyzqrs123notarealthing", quality_project, top_k=5)
        )
        assert isinstance(ctx, str)
        assert isinstance(paths, list)
        assert isinstance(count, int)

    def test_fetch_community_context_returns_three_tuple(self, quality_project):
        from opencode_search.handlers._kb_chat import _fetch_community_context
        ctx, comms, count = asyncio.run(
            _fetch_community_context("authentication search", quality_project,
                                     top_k=20, include_federation=False)
        )
        assert isinstance(ctx, str)
        assert isinstance(comms, list)
        assert isinstance(count, int)
        assert count > 0, (
            "quality_project is enriched — community context must return at least 1 community"
        )

    def test_fetch_wiki_context_returns_two_tuple(self, quality_project):
        from opencode_search.handlers._kb_chat import _fetch_wiki_context
        ctx, count = asyncio.run(
            _fetch_wiki_context("search architecture", quality_project, top_k=5)
        )
        assert isinstance(ctx, str)
        assert isinstance(count, int)
        assert count >= 0

    def test_fetch_hierarchy_communities_returns_list(self, quality_project):
        from opencode_search.handlers._kb_chat import _fetch_hierarchy_communities
        result = asyncio.run(_fetch_hierarchy_communities(quality_project, max_count=20))
        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, dict)
            assert "title" in item

    @pytest.mark.slow
    def test_quick_answer_with_real_llm(self, quality_project):
        from opencode_search.enricher import create_llm_client
        from opencode_search.handlers._kb_chat import _quick_answer
        llm = create_llm_client()
        assert llm is not None, "LLM unavailable — check OPENCODE_LLM_PROVIDER=ollama"
        answer = asyncio.run(
            _quick_answer(
                "What is the main entry point of the search engine?",
                code_ctx="src/opencode_search/mcp.py: MCP server entry point",
                comm_ctx="[feature] MCP Server: exposes 7 tools via FastMCP",
                wiki_ctx="",
                llm=llm,
                conversation_history=None,
            )
        )
        assert isinstance(answer, str)
        assert len(answer) > 10, f"_quick_answer returned too short: {answer!r}"

    @pytest.mark.slow
    def test_map_reduce_answer_with_real_llm(self, quality_project):
        from opencode_search.enricher import create_llm_client
        from opencode_search.handlers._kb_chat import _fetch_community_context, _map_reduce_answer
        llm = create_llm_client()
        assert llm is not None, "LLM unavailable — check OPENCODE_LLM_PROVIDER=ollama"
        _, comms, _ = asyncio.run(
            _fetch_community_context("search architecture", quality_project,
                                     top_k=16, include_federation=False)
        )
        assert comms, "No communities returned for map-reduce on quality_project"
        answer = asyncio.run(_map_reduce_answer(
            "List the main architectural components", comms[:8], llm
        ))
        assert isinstance(answer, str)
        assert len(answer) > 10, f"_map_reduce_answer returned too short: {answer!r}"


# ---------------------------------------------------------------------------
# handle_get_symbol_intent
# ---------------------------------------------------------------------------

class TestGetSymbolIntent:
    """handle_get_symbol_intent covers cached, missing, unbuilt-graph, and generate paths."""

    def _pick_enriched_symbol(self, quality_project: str) -> str | None:
        from opencode_search.config import get_project_graph_db_path
        db_path = get_project_graph_db_path(quality_project)
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT name FROM nodes WHERE intent IS NOT NULL LIMIT 1"
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def _pick_uncached_symbol(self, quality_project: str) -> str | None:
        from opencode_search.config import get_project_graph_db_path
        db_path = get_project_graph_db_path(quality_project)
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT name FROM nodes WHERE intent IS NULL AND kind IN ('function','method') LIMIT 1"
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def test_returns_cached_for_enriched_symbol(self, quality_project):
        from opencode_search.handlers._enrichment import handle_get_symbol_intent
        name = self._pick_enriched_symbol(quality_project)
        assert name is not None, "No enriched node found in graph.db — run enrichment first"
        result = asyncio.run(handle_get_symbol_intent(name, quality_project))
        assert "error" not in result, f"Unexpected error: {result}"
        assert result.get("cached") is True, f"Expected cached=True; got {result}"
        assert result.get("intent"), f"Expected non-empty intent; got {result}"

    def test_returns_error_for_missing_symbol(self, quality_project):
        from opencode_search.handlers._enrichment import handle_get_symbol_intent
        result = asyncio.run(
            handle_get_symbol_intent("thisDoesNotExistFooBarBaz999", quality_project)
        )
        assert "error" in result, f"Expected error key; got {result}"

    def test_returns_error_for_unbuilt_graph(self):
        from opencode_search.handlers._enrichment import handle_get_symbol_intent
        result = asyncio.run(
            handle_get_symbol_intent("any_function", "/tmp/no-graph-here-xyzqrs")
        )
        assert "error" in result, f"Expected error key; got {result}"

    @pytest.mark.slow
    def test_generates_for_uncached_symbol(self, quality_project):
        """Verifies lazy intent generation by temporarily clearing one node's intent."""
        from opencode_search.config import get_project_graph_db_path
        from opencode_search.handlers._enrichment import handle_get_symbol_intent

        # Pick an enriched symbol and temporarily clear its intent to test generation
        name = self._pick_enriched_symbol(quality_project)
        assert name is not None, "No enriched node found — graph not built"

        db_path = get_project_graph_db_path(quality_project)
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            # Save current intent and clear it
            row = conn.execute("SELECT intent FROM nodes WHERE name=? LIMIT 1", (name,)).fetchone()
            original_intent = row[0] if row else None
            conn.execute("UPDATE nodes SET intent=NULL, intent_at=NULL WHERE name=?", (name,))
            conn.commit()
        finally:
            conn.close()

        try:
            result = asyncio.run(handle_get_symbol_intent(name, quality_project))
            assert "error" not in result, f"Unexpected error: {result}"
            assert result.get("intent"), f"Expected non-empty intent; got {result}"
            assert result.get("cached") is False, f"Expected cached=False; got {result}"
        finally:
            # Restore original intent (idempotent if generation already wrote it back)
            if original_intent:
                conn2 = sqlite3.connect(db_path, timeout=2.0)
                try:
                    conn2.execute(
                        "UPDATE nodes SET intent=?, intent_at=datetime('now') WHERE name=? AND (intent IS NULL OR intent='')",
                        (original_intent, name),
                    )
                    conn2.commit()
                finally:
                    conn2.close()
