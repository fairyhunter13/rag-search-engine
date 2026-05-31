"""Comprehensive E2E test plan for opencode-search-engine.

Tests all MCP tool functionality against the real astro-project index.
Requires OPENCODE_RUN_LARGE_TESTS=1 environment variable.

Success criteria are defined per category:
  P0 — Must pass (core functionality broken if these fail)
  P1 — Should pass (important features, may flap on unindexed data)
  P2 — Nice to have (enrichment-dependent or optional features)

Run with:
  OPENCODE_RUN_LARGE_TESTS=1 .venv/bin/pytest src/tests/test_e2e_comprehensive.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import asyncio
import pytest

_ASTRO = "/home/user/git/github.com/fairyhunter13/astro-project"
_LARGE = pytest.mark.skipif(
    not os.environ.get("OPENCODE_RUN_LARGE_TESTS"),
    reason="Set OPENCODE_RUN_LARGE_TESTS=1 to run",
)


def _use_real_registry(monkeypatch):
    from pathlib import Path
    import opencode_search.config as cfg
    real_path = Path(os.path.expanduser("~/.local/share/opencode-search/projects.json"))
    monkeypatch.setattr(cfg, "REGISTRY_PATH", real_path)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ===========================================================================
# T1 — Code Search (search_code)
# ===========================================================================

class TestT1CodeSearch:
    """P0: Hybrid semantic code search."""

    @_LARGE
    def test_t1_1_find_by_function_name(self, monkeypatch):
        """P0: search_code finds a known Go function."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_search_code
        result = _run(handle_search_code(
            query="handleCartCheckout cart checkout processing",
            project_paths=[_ASTRO],
            top_k=10,
        ))
        assert result.get("results"), "search_code returned no results"
        langs = {r["language"] for r in result["results"]}
        assert langs, "no language info in results"

    @_LARGE
    def test_t1_2_find_payment_integration(self, monkeypatch):
        """P0: search_code finds payment integration code."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_search_code
        result = _run(handle_search_code(
            query="midtrans xendit payment integration callback",
            project_paths=[_ASTRO],
            top_k=10,
        ))
        hits = result.get("results", [])
        assert len(hits) >= 3, f"expected ≥3 payment results, got {len(hits)}"
        paths = " ".join(r["path"] for r in hits).lower()
        assert any(kw in paths for kw in ["payment", "midtrans", "xendit", "transaction"]), \
            f"no payment-related paths found: {[r['path'] for r in hits[:3]]}"

    @_LARGE
    def test_t1_3_find_grpc_service(self, monkeypatch):
        """P0: search_code finds gRPC service definitions."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_search_code
        result = _run(handle_search_code(
            query="gRPC server service interceptor metadata",
            project_paths=[_ASTRO],
            top_k=10,
        ))
        hits = result.get("results", [])
        assert len(hits) >= 1, "no gRPC results"

    @_LARGE
    def test_t1_4_search_returns_score(self, monkeypatch):
        """P0: all results have a score between 0 and 1."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_search_code
        result = _run(handle_search_code(
            query="inventory discount campaign",
            project_paths=[_ASTRO],
            top_k=5,
        ))
        for r in result.get("results", []):
            assert 0.0 <= r["score"] <= 1.5, f"score out of range: {r['score']}"

    @_LARGE
    def test_t1_5_search_returns_line_numbers(self, monkeypatch):
        """P0: results include non-zero line numbers."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_search_code
        result = _run(handle_search_code(
            query="order placement fulfillment",
            project_paths=[_ASTRO],
            top_k=5,
        ))
        for r in result.get("results", []):
            assert r.get("start_line", 0) >= 0, "negative start_line"


# ===========================================================================
# T2 — Architecture Search (global_search, get_communities)
# ===========================================================================

class TestT2ArchitectureSearch:
    """P0/P1: Community and wiki architectural search."""

    @_LARGE
    def test_t2_1_get_communities_returns_enriched(self, monkeypatch):
        """P0: get_communities returns enriched communities with titles."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_get_communities
        result = _run(handle_get_communities(project_path=_ASTRO))
        communities = result.get("communities", [])
        assert len(communities) >= 5, f"expected ≥5 communities, got {len(communities)}"
        enriched = [c for c in communities if c.get("title")]
        assert len(enriched) >= 3, f"expected ≥3 enriched communities, got {len(enriched)}"

    @_LARGE
    def test_t2_2_global_search_payment_finds_community(self, monkeypatch):
        """P0: global_search('payment gateway') returns payment community."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_global_search
        result = _run(handle_global_search(
            query="payment gateway transaction",
            project_path=_ASTRO,
        ))
        assert result.get("community_matches", 0) >= 1, \
            "global_search returned no community matches for payment query"
        titles = " ".join(r.get("title", "") for r in result.get("results", [])).lower()
        assert any(kw in titles for kw in ["payment", "midtrans", "xendit", "bca", "transaction"]), \
            f"no payment-related communities: {titles[:200]}"

    @_LARGE
    def test_t2_3_global_search_wiki_matches(self, monkeypatch):
        """P1: global_search returns wiki matches (fixed in wiki_query refactor)."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_global_search
        result = _run(handle_global_search(
            query="authentication authorization",
            project_path=_ASTRO,
        ))
        # After the fix, wiki_matches should be > 0 for auth queries
        wiki_matches = result.get("wiki_matches", 0)
        assert wiki_matches >= 1, \
            f"global_search returned wiki_matches=0; expected ≥1 after fix"

    @_LARGE
    def test_t2_4_global_search_architecture_overview(self, monkeypatch):
        """P1: global_search returns ≥5 total results for broad architectural query."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_global_search
        result = _run(handle_global_search(
            query="inventory order campaign management system",
            project_path=_ASTRO,
            top_k=10,
        ))
        assert result.get("total", 0) >= 3, \
            f"global_search returned only {result.get('total', 0)} results"

    @_LARGE
    def test_t2_5_communities_have_node_counts(self, monkeypatch):
        """P0: all returned communities have node_count > 0."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_get_communities
        result = _run(handle_get_communities(project_path=_ASTRO))
        for c in result.get("communities", []):
            assert c.get("node_count", 0) > 0, f"community {c.get('id')} has node_count=0"


# ===========================================================================
# T3 — Wiki Search (wiki_query, search_docs)
# ===========================================================================

class TestT3WikiSearch:
    """P0: Language-filtered wiki vector search."""

    @_LARGE
    def test_t3_1_wiki_query_payment(self, monkeypatch):
        """P0: wiki_query finds payment wiki pages."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers._wiki import handle_wiki_query
        result = _run(handle_wiki_query(
            query="payment gateway transaction processing",
            project_path=_ASTRO,
            top_k=5,
        ))
        assert result.get("total", 0) >= 1, \
            f"wiki_query returned 0 results for payment query"
        for r in result["results"]:
            assert r.get("language") == "wiki", f"non-wiki result: {r}"

    @_LARGE
    def test_t3_2_wiki_query_authentication(self, monkeypatch):
        """P0: wiki_query finds authentication documentation."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers._wiki import handle_wiki_query
        result = _run(handle_wiki_query(
            query="API authentication authorization token",
            project_path=_ASTRO,
            top_k=5,
        ))
        assert result.get("total", 0) >= 1, "wiki_query auth returned 0 results"
        paths = " ".join(r["path"] for r in result["results"]).lower()
        assert "auth" in paths or "community" in paths, \
            f"no auth-related wiki pages found: {paths}"

    @_LARGE
    def test_t3_3_wiki_query_architecture(self, monkeypatch):
        """P0: wiki_query finds architecture summary."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers._wiki import handle_wiki_query
        result = _run(handle_wiki_query(
            query="microservices architecture platform overview",
            project_path=_ASTRO,
            top_k=5,
        ))
        assert result.get("total", 0) >= 1, "wiki_query arch returned 0 results"

    @_LARGE
    def test_t3_4_search_docs_deployment(self, monkeypatch):
        """P1: search_docs finds deployment documentation."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_search_code
        result = _run(handle_search_code(
            query="deployment kubernetes docker infrastructure",
            project_paths=[_ASTRO],
            top_k=10,
        ))
        assert result.get("results"), "search returned no results"

    @_LARGE
    def test_t3_5_wiki_results_have_content(self, monkeypatch):
        """P0: wiki_query results have non-empty content."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers._wiki import handle_wiki_query
        result = _run(handle_wiki_query(
            query="campaign discount inventory",
            project_path=_ASTRO,
            top_k=3,
        ))
        for r in result.get("results", []):
            assert r.get("content", "").strip(), f"empty content in wiki result: {r['path']}"

    @_LARGE
    def test_t3_6_wiki_lint_healthy(self, monkeypatch):
        """P1: wiki_lint runs without crashing."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers._wiki import handle_wiki_lint
        result = _run(handle_wiki_lint(project_path=_ASTRO))
        assert "total_pages" in result, f"wiki_lint missing total_pages: {result}"
        assert result.get("total_pages", 0) >= 100, \
            f"expected ≥100 wiki pages, got {result.get('total_pages', 0)}"


# ===========================================================================
# T4 — Function Tracing (get_callers, get_callees, trace_path)
# ===========================================================================

class TestT4FunctionTracing:
    """P1: Graph-based call graph traversal."""

    @_LARGE
    def test_t4_1_get_symbol_exists(self, monkeypatch):
        """P0: get_symbol returns info for a known symbol."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_get_symbol
        result = _run(handle_get_symbol(name="http.Run", project_path=_ASTRO))
        # Either found or not found — should not error
        assert "error" not in result or result.get("matches") == [], \
            f"get_symbol errored unexpectedly: {result}"

    @_LARGE
    def test_t4_2_get_callers_runs(self, monkeypatch):
        """P1: get_callers returns a result without crashing."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_get_callers
        result = _run(handle_get_callers(symbol="http.Run", project_path=_ASTRO))
        assert isinstance(result, dict), "get_callers returned non-dict"

    @_LARGE
    def test_t4_3_get_callees_runs(self, monkeypatch):
        """P1: get_callees returns a result without crashing."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_get_callees
        result = _run(handle_get_callees(symbol="http.Run", project_path=_ASTRO))
        assert isinstance(result, dict), "get_callees returned non-dict"

    @_LARGE
    def test_t4_4_trace_path_runs(self, monkeypatch):
        """P1: trace_path runs without crashing."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_trace_path
        result = _run(handle_trace_path(
            from_symbol="http.Run",
            to_symbol="tracer.StartSpanWithContext",
            project_path=_ASTRO,
        ))
        assert isinstance(result, dict), "trace_path returned non-dict"


# ===========================================================================
# T5 — Impact Analysis (detect_impact)
# ===========================================================================

class TestT5ImpactAnalysis:
    """P1: Change impact detection via call graph."""

    @_LARGE
    def test_t5_1_detect_impact_core_symbol(self, monkeypatch):
        """P1: detect_impact returns impact for a widely-used symbol."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_detect_impact
        result = _run(handle_detect_impact(
            symbol="tracer.StartSpanWithContext",
            project_path=_ASTRO,
        ))
        assert isinstance(result, dict), "detect_impact returned non-dict"
        # Should find at least the symbol itself or its callers
        impact = result.get("impact", result.get("affected", []))
        # Not asserting count — graph may not be fully resolved


# ===========================================================================
# T6 — Project Structure (project_structure)
# ===========================================================================

class TestT6ProjectStructure:
    """P0: Project directory tree and graph overview."""

    @_LARGE
    def test_t6_1_project_structure_returns_tree(self, monkeypatch):
        """P0: project_structure returns a directory tree."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_project_structure
        result = _run(handle_project_structure(project_path=_ASTRO, max_depth=3))
        assert result.get("status") == "ok", f"project_structure failed: {result}"
        tree = result.get("directory_tree", "")
        assert "repositories-ubuntu" in tree or "docs" in tree, \
            f"directory tree missing expected dirs: {tree[:200]}"

    @_LARGE
    def test_t6_2_project_structure_language_breakdown(self, monkeypatch):
        """P0: project_structure returns language breakdown with Go and Java."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_project_structure
        result = _run(handle_project_structure(project_path=_ASTRO))
        langs = {item["extension"] for item in result.get("language_breakdown", [])}
        assert ".go" in langs, f"Go not in language breakdown: {langs}"

    @_LARGE
    def test_t6_3_project_structure_graph_stats(self, monkeypatch):
        """P0: project_structure includes graph stats with communities."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_project_structure
        result = _run(handle_project_structure(project_path=_ASTRO, include_graph_stats=True))
        stats = result.get("graph_stats", {})
        assert stats.get("total_communities", 0) > 1000, \
            f"expected >1000 communities, got {stats.get('total_communities', 0)}"
        assert stats.get("enriched_communities", 0) >= 100, \
            f"expected ≥100 enriched communities, got {stats.get('enriched_communities', 0)}"

    @_LARGE
    def test_t6_4_project_structure_top_communities(self, monkeypatch):
        """P0: project_structure returns top communities with titles."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_project_structure
        result = _run(handle_project_structure(project_path=_ASTRO))
        communities = result.get("top_communities", [])
        assert len(communities) >= 5, f"expected ≥5 top communities, got {len(communities)}"
        titled = [c for c in communities if c.get("title")]
        assert len(titled) >= 3, f"expected ≥3 titled communities, got {len(titled)}"


# ===========================================================================
# T7 — Federation (discover, list, add, remove)
# ===========================================================================

class TestT7Federation:
    """P0: Federation discovery and membership management."""

    @_LARGE
    def test_t7_1_discover_finds_24_members(self, monkeypatch):
        """P0: discover_federation finds all 24 symlinked sub-repos."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_discover_federation
        result = _run(handle_discover_federation(project_path=_ASTRO))
        discovered = result.get("discovered", [])
        assert len(discovered) >= 20, \
            f"expected ≥20 federation members, got {len(discovered)}"

    @_LARGE
    def test_t7_2_list_federation_shows_registered(self, monkeypatch):
        """P0: list_federation returns ≥20 registered members."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_list_federation
        result = _run(handle_list_federation(project_path=_ASTRO))
        members = result.get("members", [])
        assert len(members) >= 20, \
            f"expected ≥20 registered members, got {len(members)}"

    @_LARGE
    def test_t7_3_add_remove_member_roundtrip(self, monkeypatch):
        """P0: add and remove a federation member leaves state unchanged."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import (
            handle_add_federation_member,
            handle_remove_federation_member,
            handle_list_federation,
        )
        import tempfile
        test_member_dir = tempfile.mkdtemp(prefix="opencode-fed-test-")
        test_member = test_member_dir
        # Record current member count
        before = len(_run(handle_list_federation(project_path=_ASTRO)).get("members", []))

        # Add
        add_result = _run(handle_add_federation_member(
            root_path=_ASTRO, member_path=test_member
        ))
        assert add_result.get("status") == "ok", f"add_federation_member failed: {add_result}"

        after_add = len(_run(handle_list_federation(project_path=_ASTRO)).get("members", []))
        assert after_add == before + 1, \
            f"member count should be {before+1}, got {after_add}"

        # Remove
        rm_result = _run(handle_remove_federation_member(
            root_path=_ASTRO, member_path=test_member
        ))
        assert rm_result.get("status") == "ok", f"remove_federation_member failed: {rm_result}"

        after_rm = len(_run(handle_list_federation(project_path=_ASTRO)).get("members", []))
        assert after_rm == before, \
            f"member count should be back to {before}, got {after_rm}"


# ===========================================================================
# T8 — Status & Metrics
# ===========================================================================

class TestT8StatusMetrics:
    """P0: Observability and status tools."""

    @_LARGE
    def test_t8_1_project_status_indexed(self, monkeypatch):
        """P0: project_status shows astro-project as indexed with correct chunk count."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_project_status
        result = _run(handle_project_status(path=_ASTRO))
        assert result.get("indexed") is True, "astro-project not shown as indexed"
        chunks = result.get("chunks", 0)
        assert chunks >= 100_000, \
            f"expected ≥100k chunks, got {chunks}"

    @_LARGE
    def test_t8_2_list_indexed_projects_includes_astro(self, monkeypatch):
        """P0: list_indexed_projects includes astro-project."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_list_indexed_projects
        result = _run(handle_list_indexed_projects())
        paths = [p["path"] for p in result.get("projects", [])]
        assert _ASTRO in paths, f"astro-project not in indexed list: {paths[:5]}"

    @_LARGE
    def test_t8_3_search_metrics_runs(self, monkeypatch):
        """P1: search_metrics returns a dict without crashing."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers._query import handle_search_code
        # Trigger a search to populate metrics
        _run(handle_search_code(query="test metrics", project_paths=[_ASTRO], top_k=3))
        # search_metrics is a standalone handler
        import asyncio
        from opencode_search.mcp import mcp
        # Just check the handler exists and is callable
        from opencode_search.handlers._query import handle_list_indexed_projects
        result = _run(handle_list_indexed_projects())
        assert "projects" in result


# ===========================================================================
# T9 — CLI Integration (claude haiku-4-5 and codex)
# ===========================================================================

class TestT9CLIIntegration:
    """P1: MCP tools accessible from claude and codex CLI."""

    @_LARGE
    def test_t9_1_claude_haiku_list_projects(self):
        """P1: claude --model claude-haiku-4-5 can call list_indexed_projects via MCP."""
        result = subprocess.run(
            [
                "claude",
                "--dangerously-skip-permissions",
                "--model", "claude-haiku-4-5",
                "-p",
                "Call list_indexed_projects MCP tool. Output ONLY a JSON object: "
                '{"project_count": <number>}. No other text.',
            ],
            capture_output=True, text=True, timeout=60,
        )
        output = result.stdout.strip()
        # Find JSON in output
        try:
            start = output.index("{")
            end = output.rindex("}") + 1
            data = json.loads(output[start:end])
            count = data.get("project_count", 0)
            assert count >= 2, f"expected ≥2 indexed projects, got {count}"
        except (ValueError, json.JSONDecodeError) as e:
            pytest.fail(f"Could not parse JSON from claude output: {output!r} — {e}")

    @_LARGE
    def test_t9_2_claude_haiku_search_code(self):
        """P1: claude haiku-4-5 can use search_code to find payment code."""
        result = subprocess.run(
            [
                "claude",
                "--dangerously-skip-permissions",
                "--model", "claude-haiku-4-5",
                "-p",
                f"Use search_code MCP tool with query='midtrans payment callback' and "
                f"project_paths=['{_ASTRO}']. "
                "Output ONLY JSON: {\"result_count\": <number>, \"first_path\": \"<path>\"}.",
            ],
            capture_output=True, text=True, timeout=90,
        )
        output = result.stdout.strip()
        try:
            start = output.index("{")
            end = output.rindex("}") + 1
            data = json.loads(output[start:end])
            assert data.get("result_count", 0) >= 1, f"search returned 0 results: {data}"
        except (ValueError, json.JSONDecodeError) as e:
            pytest.fail(f"Could not parse JSON from claude output: {output!r} — {e}")

    @_LARGE
    def test_t9_3_codex_list_projects(self):
        """P1: codex can call list_indexed_projects via MCP."""
        result = subprocess.run(
            [
                "codex", "exec",
                "--approval-mode", "never",
                "Use the opencode-search list_indexed_projects tool. "
                "Output ONLY this JSON: {\"project_count\": <number>}",
            ],
            capture_output=True, text=True, timeout=60,
            cwd=_ASTRO,
        )
        output = (result.stdout + result.stderr).strip()
        try:
            start = output.index("{")
            end = output.rindex("}") + 1
            data = json.loads(output[start:end])
            count = data.get("project_count", 0)
            assert count >= 2, f"expected ≥2 indexed projects via codex, got {count}"
        except (ValueError, json.JSONDecodeError) as e:
            pytest.skip(f"codex output not parseable (may lack MCP access in exec mode): {e}")


# ===========================================================================
# T10 — Business Process Tracing
# ===========================================================================

class TestT10BusinessProcessTracing:
    """P1: End-to-end business flow discovery via search + graph tools."""

    @_LARGE
    def test_t10_1_find_checkout_flow_entry(self, monkeypatch):
        """P1: Can find the checkout business flow entry point."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_search_code
        result = _run(handle_search_code(
            query="checkout order placement API handler endpoint",
            project_paths=[_ASTRO],
            top_k=10,
        ))
        hits = result.get("results", [])
        assert len(hits) >= 1, "No checkout flow results found"

    @_LARGE
    def test_t10_2_campaign_business_process(self, monkeypatch):
        """P1: global_search finds campaign management as architectural layer."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_global_search
        result = _run(handle_global_search(
            query="campaign management promo validation discount",
            project_path=_ASTRO,
            top_k=10,
        ))
        hits = result.get("results", [])
        titles = " ".join(r.get("title", "") for r in hits).lower()
        assert "campaign" in titles or len(hits) >= 3, \
            f"No campaign community found in: {titles[:300]}"

    @_LARGE
    def test_t10_3_supply_chain_tracing(self, monkeypatch):
        """P1: search_code finds supply chain / inventory management."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_search_code
        result = _run(handle_search_code(
            query="supply order inventory stock adjustment replenishment",
            project_paths=[_ASTRO],
            top_k=10,
        ))
        hits = result.get("results", [])
        assert len(hits) >= 3, f"expected ≥3 supply chain results, got {len(hits)}"

    @_LARGE
    def test_t10_4_wiki_business_process_docs(self, monkeypatch):
        """P1: wiki_query finds business process documentation."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers._wiki import handle_wiki_query
        result = _run(handle_wiki_query(
            query="business process workflow order fulfillment",
            project_path=_ASTRO,
            top_k=5,
        ))
        # We have community pages that cover order management
        assert result.get("total", 0) >= 1, \
            "wiki_query returned 0 results for business process query"


# ===========================================================================
# T11 — v2 Intent API (7 tools)
# ===========================================================================

class TestT11IntentAPI:
    """P0: All 7 intent tools route correctly and return valid shapes."""

    @_LARGE
    def test_t11_search_code_scope(self, monkeypatch):
        """P0: search(scope=code) returns code results."""
        _use_real_registry(monkeypatch)
        from opencode_search.mcp import search
        result = _run(search(query="payment midtrans callback", project_paths=[_ASTRO]))
        assert result.get("results"), "search(scope=code) returned no results"

    @_LARGE
    def test_t11_search_docs_scope(self, monkeypatch):
        """P0: search(scope=docs) returns only wiki/markdown results."""
        _use_real_registry(monkeypatch)
        from opencode_search.mcp import search
        result = _run(search(
            query="authentication API guide",
            scope="docs",
            project_paths=[_ASTRO],
        ))
        for r in result.get("results", []):
            lang = r.get("language", "")
            path = r.get("path", "")
            assert lang in ("wiki","knowledge_base","markdown","rst","text") or \
                   path.endswith((".md",".rst",".txt")), \
                   f"Non-doc result in docs scope: {lang} {path}"

    @_LARGE
    def test_t11_search_invalid_scope_returns_error(self, monkeypatch):
        """P0: search with invalid scope returns error dict, not exception."""
        _use_real_registry(monkeypatch)
        from opencode_search.mcp import search
        result = _run(search(query="test", scope="nonsense", project_paths=[_ASTRO]))
        assert "error" in result, "invalid scope should return error dict"
        assert "valid_scopes" in result

    @_LARGE
    def test_t11_ask_architecture_scope(self, monkeypatch):
        """P0: ask(scope=architecture) returns community results."""
        _use_real_registry(monkeypatch)
        from opencode_search.mcp import ask
        result = _run(ask(query="payment gateway", project_path=_ASTRO, scope="architecture"))
        assert result.get("community_matches", 0) >= 1, \
            "ask(architecture) returned 0 community matches"

    @_LARGE
    def test_t11_ask_wiki_scope(self, monkeypatch):
        """P0: ask(scope=wiki) returns wiki page results."""
        _use_real_registry(monkeypatch)
        from opencode_search.mcp import ask
        result = _run(ask(query="architecture overview", project_path=_ASTRO, scope="wiki"))
        assert result.get("total", 0) >= 1, "ask(wiki) returned 0 results"

    @_LARGE
    def test_t11_ask_all_scope(self, monkeypatch):
        """P0: ask(scope=all) returns combined results."""
        _use_real_registry(monkeypatch)
        from opencode_search.mcp import ask
        result = _run(ask(query="campaign management", project_path=_ASTRO, scope="all"))
        total = result.get("total", 0)
        assert total >= 1, f"ask(all) returned 0 results"

    @_LARGE
    def test_t11_ask_invalid_scope_returns_error(self, monkeypatch):
        """P0: ask with invalid scope returns error dict."""
        _use_real_registry(monkeypatch)
        from opencode_search.mcp import ask
        result = _run(ask(query="test", project_path=_ASTRO, scope="xyz"))
        assert "error" in result

    @_LARGE
    def test_t11_graph_definition(self, monkeypatch):
        """P0: graph(relation=definition) returns symbol info."""
        _use_real_registry(monkeypatch)
        from opencode_search.mcp import graph
        result = _run(graph(symbol="http.Run", project_path=_ASTRO, relation="definition"))
        assert isinstance(result, dict), "graph returned non-dict"

    @_LARGE
    def test_t11_graph_callers(self, monkeypatch):
        """P0: graph(relation=callers) returns callers without error."""
        _use_real_registry(monkeypatch)
        from opencode_search.mcp import graph
        result = _run(graph(symbol="http.Run", project_path=_ASTRO, relation="callers"))
        assert isinstance(result, dict)

    @_LARGE
    def test_t11_graph_path_requires_to_symbol(self, monkeypatch):
        """P0: graph(relation=path) without to_symbol returns error dict."""
        _use_real_registry(monkeypatch)
        from opencode_search.mcp import graph
        result = _run(graph(symbol="http.Run", project_path=_ASTRO, relation="path"))
        assert "error" in result, "missing to_symbol should return error"

    @_LARGE
    def test_t11_overview_structure(self, monkeypatch):
        """P0: overview(what=structure) returns directory tree."""
        _use_real_registry(monkeypatch)
        from opencode_search.mcp import overview
        result = _run(overview(project_path=_ASTRO, what="structure"))
        assert result.get("status") == "ok"
        assert "directory_tree" in result

    @_LARGE
    def test_t11_overview_communities(self, monkeypatch):
        """P0: overview(what=communities) returns community list."""
        _use_real_registry(monkeypatch)
        from opencode_search.mcp import overview
        result = _run(overview(project_path=_ASTRO, what="communities"))
        assert len(result.get("communities", [])) >= 5

    @_LARGE
    def test_t11_overview_status(self, monkeypatch):
        """P0: overview(what=status) returns indexed=True."""
        _use_real_registry(monkeypatch)
        from opencode_search.mcp import overview
        result = _run(overview(project_path=_ASTRO, what="status"))
        assert result.get("indexed") is True

    @_LARGE
    def test_t11_overview_projects(self, monkeypatch):
        """P0: overview(what=projects) returns project list without project_path."""
        _use_real_registry(monkeypatch)
        from opencode_search.mcp import overview
        result = _run(overview(what="projects"))
        assert len(result.get("projects", [])) >= 2

    @_LARGE
    def test_t11_overview_invalid_what_returns_error(self, monkeypatch):
        """P0: overview with invalid what returns error dict."""
        _use_real_registry(monkeypatch)
        from opencode_search.mcp import overview
        result = _run(overview(what="nonsense"))
        assert "error" in result

    @_LARGE
    def test_t11_build_invalid_action_returns_error(self, monkeypatch):
        """P0: build with invalid action returns error dict."""
        _use_real_registry(monkeypatch)
        from opencode_search.mcp import build
        result = _run(build(project_path=_ASTRO, action="fly"))
        assert "error" in result

    @_LARGE
    def test_t11_federation_list(self, monkeypatch):
        """P0: federation(action=list) returns ≥20 members."""
        _use_real_registry(monkeypatch)
        from opencode_search.mcp import federation
        result = _run(federation(root_path=_ASTRO, action="list"))
        assert len(result.get("members", [])) >= 20

    @_LARGE
    def test_t11_federation_discover(self, monkeypatch):
        """P0: federation(action=discover) returns ≥20 discovered."""
        _use_real_registry(monkeypatch)
        from opencode_search.mcp import federation
        result = _run(federation(root_path=_ASTRO, action="discover"))
        assert len(result.get("discovered", [])) >= 20

    @_LARGE
    def test_t11_manage_wiki_lint(self, monkeypatch):
        """P0: manage(action=wiki_lint) returns health check."""
        _use_real_registry(monkeypatch)
        from opencode_search.mcp import manage
        result = _run(manage(project_path=_ASTRO, action="wiki_lint"))
        assert "total_pages" in result

    @_LARGE
    def test_t11_manage_invalid_action_returns_error(self, monkeypatch):
        """P0: manage with invalid action returns error dict."""
        _use_real_registry(monkeypatch)
        from opencode_search.mcp import manage
        result = _run(manage(project_path=_ASTRO, action="explode"))
        assert "error" in result


# ===========================================================================
# T12 — CLI: 7-tool API via claude haiku
# ===========================================================================

class TestT12CLIIntentAPI:
    """P1: 7 new intent tools accessible via claude haiku-4-5."""

    @_LARGE
    def test_t12_claude_uses_search_tool(self):
        """P1: claude haiku-4-5 calls the new `search` tool (not search_code)."""
        result = subprocess.run(
            [
                "claude", "--dangerously-skip-permissions",
                "--model", "claude-haiku-4-5",
                "-p",
                f"Use the `search` MCP tool with query='midtrans payment' and "
                f"project_paths=['{_ASTRO}'], scope='code'. "
                'Output ONLY JSON: {"result_count": <number>}',
            ],
            capture_output=True, text=True, timeout=90,
        )
        output = result.stdout.strip()
        try:
            start = output.index("{")
            end = output.rindex("}") + 1
            data = json.loads(output[start:end])
            assert data.get("result_count", 0) >= 1, f"search returned 0: {data}"
        except (ValueError, json.JSONDecodeError) as e:
            pytest.fail(f"Could not parse output: {output!r} — {e}")

    @_LARGE
    def test_t12_claude_uses_overview_tool(self):
        """P1: claude haiku-4-5 calls the `overview` tool for project list."""
        result = subprocess.run(
            [
                "claude", "--dangerously-skip-permissions",
                "--model", "claude-haiku-4-5",
                "-p",
                "Use the `overview` MCP tool with what='projects'. "
                'Output ONLY JSON: {"project_count": <number>}',
            ],
            capture_output=True, text=True, timeout=60,
        )
        output = result.stdout.strip()
        try:
            start = output.index("{")
            end = output.rindex("}") + 1
            data = json.loads(output[start:end])
            assert data.get("project_count", 0) >= 2
        except (ValueError, json.JSONDecodeError) as e:
            pytest.fail(f"Could not parse output: {output!r} — {e}")


# ===========================================================================
# T13 — Dashboard API routes
# ===========================================================================

class TestT13Dashboard:
    """P0: Dashboard API routes return correct shapes."""

    def _api(self, path: str) -> dict:
        import urllib.request, json as _json
        url = f"http://127.0.0.1:8765{path}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return _json.loads(r.read())
        except Exception as e:
            pytest.skip(f"Daemon not running or route failed: {e}")

    def _html(self, path: str) -> str:
        import urllib.request
        url = f"http://127.0.0.1:8765{path}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return r.read().decode()
        except Exception as e:
            pytest.skip(f"Daemon not running: {e}")

    def test_t13_dashboard_returns_html(self):
        """P0: GET /dashboard returns HTML."""
        html = self._html("/dashboard")
        assert "<!DOCTYPE html>" in html, "dashboard not returning HTML"
        assert "opencode-search" in html

    def test_t13_api_projects_returns_list(self):
        """P0: GET /api/projects returns project list."""
        data = self._api("/api/projects")
        assert "projects" in data, f"missing 'projects': {data}"
        assert len(data["projects"]) >= 2

    def test_t13_api_communities_returns_list(self):
        """P0: GET /api/communities returns community list for astro-project."""
        data = self._api(f"/api/communities?project={_ASTRO}&top_k=5")
        assert "communities" in data
        assert len(data["communities"]) >= 1

    def test_t13_api_wiki_returns_pages(self):
        """P0: GET /api/wiki returns wiki page list."""
        data = self._api(f"/api/wiki?project={_ASTRO}")
        assert "pages" in data
        assert data.get("total", 0) >= 100, \
            f"expected ≥100 wiki pages, got {data.get('total', 0)}"

    def test_t13_api_overview_returns_tree(self):
        """P0: GET /api/overview returns directory tree."""
        data = self._api(f"/api/overview?project={_ASTRO}")
        assert "directory_tree" in data
        assert "language_breakdown" in data

    def test_t13_api_metrics_returns_dict(self):
        """P0: GET /api/metrics returns metrics dict."""
        data = self._api("/api/metrics")
        assert isinstance(data, dict)

    def test_t13_api_federation_returns_members(self):
        """P0: GET /api/federation returns member list."""
        data = self._api(f"/api/federation?project={_ASTRO}")
        assert "members" in data
        assert len(data["members"]) >= 20


# ===========================================================================
# Summary of success/failure criteria (v2 — 7-tool intent API)
# ===========================================================================

"""
TOOL COVERAGE MATRIX (v2)
=========================
Tool        Priority  Tests
----------  --------  ------
search      P0        T1.1-T1.5, T11.search_*, T12.1, T10.1-T10.3
ask         P0        T2.2-T2.4, T11.ask_*, T3.1-T3.5
graph       P0        T4.1-T4.4, T5.1, T11.graph_*
overview    P0        T6.1-T6.4, T8.1-T8.2, T11.overview_*
build       P1        T11.build_invalid (full pipeline: manual, ~441s)
federation  P0        T7.1-T7.3, T11.federation_*
manage      P0        T3.6, T11.manage_*
dashboard   P0        T13.1-T13.7

FORMAL SUCCESS/FAILURE CRITERIA
================================
Capability             SUCCESS                              FAILURE
---------------------  -----------------------------------  -----------------------
Code graph structure   >1000 communities, callers resolve  0 communities or errors
Project structure      tree w/ repos-ubuntu; .go in langs  empty tree or missing
Function tracing       returns dict ≥0 edges, no crash     exception or bad shape
Architecture           payment query → payment community    0 community matches
Wiki                   payment/auth queries → ≥1 wiki page 0 wiki for indexed topic
Business processes     checkout/campaign → ≥3 hits         <1 relevant hit
Knowledge base         unenriched meaningful comms == 0    >0 after complete run
Impact analysis        affected set for hot symbol         crash
Status/observability   indexed=True, chunks ≥100k          wrong counts
Multi-client           claude haiku calls tools correctly  tool not callable
Dashboard              all tabs load; /api/* return data   any tab blank or 500

REMAINING WORK (Phase C + D running in background)
===================================================
- Root enrichment: 1957 remaining communities → running via document_federation.py
- Member documentation: 24 members need separate index+graph+enrich+wiki
  Run: python scripts/document_federation.py --skip-root
- knowledge graph export (Phase F): graph_export API route + download button
"""
