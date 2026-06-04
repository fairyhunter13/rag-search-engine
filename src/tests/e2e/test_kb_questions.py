"""E2E tests: KB can answer the four comprehensive question types.

Verifies that after a full pipeline build with local Ollama, the KB can answer:
  Q1: "What are the features and business processes?" (scope=global)
  Q2: "Which code is related to these features?" (search)
  Q3: "Why is it designed this way?" (scope=feature)
  Q4: "How is the call graph / tracing of core features?" (graph callers/callees)

All answers must be comprehensive: non-empty, contain code file references,
and include the required fields for each scope.

Markers: runtime_deps (requires live daemon + indexed project + Ollama GPU).
"""
from __future__ import annotations

import asyncio
import pytest

PROJECT_PATH = "/home/user/git/github.com/fairyhunter13/opencode-search-engine"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _daemon_reachable() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/", timeout=3):
            return True
    except Exception:
        return False


def _ollama_reachable() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3):
            return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Fixtures / marks
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.runtime_deps


@pytest.fixture(scope="module", autouse=True)
def require_live_services():
    if not _daemon_reachable():
        pytest.skip("daemon not running on :8765")
    if not _ollama_reachable():
        pytest.skip("Ollama not running on :11434")


# ---------------------------------------------------------------------------
# Q1: Features and business processes — scope=global (MAP-REDUCE)
# ---------------------------------------------------------------------------

class TestQ1FeaturesAndBusinessProcesses:
    """The KB must give a comprehensive, structured answer about all features."""

    @pytest.fixture(scope="class")
    def result(self):
        from opencode_search.handlers._global_search import handle_global_synthesis
        return _run(handle_global_synthesis(
            query="what are all the features and business processes of this codebase? list exhaustively with code locations",
            project_path=PROJECT_PATH,
        ))

    def test_returns_non_empty_answer(self, result):
        answer = result.get("answer", "")
        assert len(answer) > 200, f"Answer too short: {len(answer)} chars"

    def test_answer_mentions_search_feature(self, result):
        answer = result.get("answer", "").lower()
        assert any(kw in answer for kw in ["search", "index", "embed"]), \
            "Answer should mention core search/indexing feature"

    def test_answer_mentions_graph_feature(self, result):
        answer = result.get("answer", "").lower()
        assert any(kw in answer for kw in ["graph", "community", "call"]), \
            "Answer should mention graph/community detection feature"

    def test_answer_mentions_llm_feature(self, result):
        answer = result.get("answer", "").lower()
        assert any(kw in answer for kw in ["llm", "enrich", "pipeline", "knowledge"]), \
            "Answer should mention LLM enrichment/pipeline feature"

    def test_community_count_is_substantial(self, result):
        count = result.get("community_count", 0)
        assert count >= 10, f"Expected at least 10 communities, got {count}"

    def test_used_map_reduce(self, result):
        assert result.get("map_batches", 0) >= 1, "Expected MAP-REDUCE to be used"

    def test_elapsed_is_reasonable(self, result):
        elapsed = result.get("elapsed_ms", 0)
        assert elapsed < 120_000, f"Query took too long: {elapsed}ms"


# ---------------------------------------------------------------------------
# Q2: Which code is related to these features? — search
# ---------------------------------------------------------------------------

class TestQ2CodeLocations:
    """Search must return specific file paths and scored results."""

    @pytest.fixture(scope="class")
    def search_results(self):
        from opencode_search.handlers._query import handle_search_code
        return _run(handle_search_code(
            query="knowledge base pipeline enrichment LLM community detection",
            project_paths=[PROJECT_PATH],
            top_k=10,
        ))

    def test_returns_results(self, search_results):
        results = search_results.get("results", [])
        assert len(results) >= 3, f"Expected at least 3 results, got {len(results)}"

    def test_results_have_file_paths(self, search_results):
        results = search_results.get("results", [])
        for r in results:
            assert r.get("path"), f"Result missing path: {r}"
            assert r["path"].endswith(".py") or "/" in r["path"], \
                f"Expected Python file path, got: {r['path']}"

    def test_results_have_scores(self, search_results):
        results = search_results.get("results", [])
        for r in results:
            assert r.get("score", -1) >= 0, f"Score must be non-negative: {r.get('score')}"

    def test_top_result_is_relevant(self, search_results):
        results = search_results.get("results", [])
        assert results, "No results"
        top_path = results[0]["path"].lower()
        assert any(kw in top_path for kw in ["pipeline", "enrich", "handler", "community", "llm"]), \
            f"Top result doesn't seem relevant: {top_path}"

    def test_handler_files_appear(self, search_results):
        paths = [r["path"] for r in search_results.get("results", [])]
        has_handler = any("handler" in p or "enrichment" in p or "pipeline" in p for p in paths)
        assert has_handler, f"Expected handler files in results, got: {paths}"


# ---------------------------------------------------------------------------
# Q3: Why is it designed this way? — scope=feature
# ---------------------------------------------------------------------------

class TestQ3DesignRationale:
    """Feature trace must return algorithm + design_rationale + entry points."""

    @pytest.fixture(scope="class")
    def result(self):
        from opencode_search.handlers._feature import handle_ask_feature
        return _run(handle_ask_feature(
            query="why is the knowledge base pipeline designed with multi-tier LLM and community detection?",
            project_path=PROJECT_PATH,
            top_k=15,
        ))

    def test_status_ok(self, result):
        assert result.get("status") == "ok", f"Unexpected status: {result.get('status')}"

    def test_has_entry_points(self, result):
        eps = result.get("entry_points", [])
        assert len(eps) >= 1, "Expected at least 1 entry point"

    def test_entry_points_have_file_paths(self, result):
        eps = result.get("entry_points", [])
        for ep in eps:
            assert ep.get("file"), f"Entry point missing file: {ep}"

    def test_has_algorithm(self, result):
        algo = result.get("algorithm")
        assert algo and len(algo) > 30, f"Algorithm too short or missing: {algo!r}"

    def test_has_design_rationale(self, result):
        rationale = result.get("design_rationale")
        assert rationale and len(rationale) > 30, \
            f"Design rationale too short or missing: {rationale!r}"

    def test_has_call_chain(self, result):
        chain = result.get("call_chain", [])
        assert len(chain) >= 1, "Expected at least 1 call chain entry"

    def test_involved_services_populated(self, result):
        services = result.get("involved_services", [])
        assert len(services) >= 1, "Expected at least 1 involved service"


# ---------------------------------------------------------------------------
# Q4: Call graph / tracing of core features — graph callers/callees
# ---------------------------------------------------------------------------

class TestQ4CallGraph:
    """Graph queries must return call chains with file paths and confidence."""

    @pytest.fixture(scope="class")
    def callees_result(self):
        from opencode_search.handlers._graph import handle_get_callees
        return _run(handle_get_callees(
            symbol="handle_pipeline",
            project_path=PROJECT_PATH,
            depth=3,
        ))

    @pytest.fixture(scope="class")
    def callers_result(self):
        from opencode_search.handlers._graph import handle_get_callers
        return _run(handle_get_callers(
            symbol="handle_search_code",
            project_path=PROJECT_PATH,
            depth=2,
        ))

    def test_callees_returns_results(self, callees_result):
        callees = callees_result.get("callees", [])
        assert len(callees) >= 1, "Expected at least 1 callee for handle_pipeline"

    def test_callees_have_file_paths(self, callees_result):
        for c in callees_result.get("callees", []):
            assert c.get("file"), f"Callee missing file: {c}"

    def test_callees_have_confidence(self, callees_result):
        for c in callees_result.get("callees", []):
            assert 0 <= c.get("confidence", -1) <= 1, \
                f"Confidence out of range: {c.get('confidence')}"

    def test_callers_returns_results(self, callers_result):
        callers = callers_result.get("callers", [])
        assert len(callers) >= 3, \
            f"Expected at least 3 callers for handle_search_code, got {len(callers)}"

    def test_callers_include_kb_chat(self, callers_result):
        caller_names = [c.get("qualified_name", "") for c in callers_result.get("callers", [])]
        assert any("kb_chat" in n or "feature" in n or "global" in n for n in caller_names), \
            f"Expected kb_chat/feature/global in callers, got: {caller_names[:5]}"

    def test_callers_have_depth(self, callers_result):
        depths = {c.get("depth") for c in callers_result.get("callers", [])}
        assert depths, "No depth info in callers"
        assert max(depths) >= 1, "Expected callers at depth >= 1"

    def test_pipeline_trace_path(self):
        from opencode_search.handlers._graph import handle_trace_path
        result = _run(handle_trace_path(
            from_symbol="handle_pipeline",
            to_symbol="resolve",
            project_path=PROJECT_PATH,
        ))
        assert result.get("connected") is True or result.get("path") is not None, \
            "Expected a trace path between handle_pipeline and resolve"
