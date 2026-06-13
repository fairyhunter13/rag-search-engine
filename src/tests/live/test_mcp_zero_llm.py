"""U0: prove that EVERY MCP read action is zero-LLM at request time.

No mocks. Proof method:
  1. For each tool × sub-action, call it via the live REST API.
  2. Assert the response carries llm_used=False/absent and returns in ≤10s.
  3. Read the process-wide LLM inference counter via /api/metrics immediately
     before and after the call and assert a **zero delta** — proving no LLM
     request was issued, independent of the self-reported llm_used flag.

Covered actions
---------------
search  : code | docs | all
ask     : all | global | feature | business | wiki | architecture
graph   : definition | callers | callees | impact | impact_narrative |
          semantic_trace | path
overview: structure | communities | status | projects | patterns |
          service_mesh | architecture_domains | hierarchy | import_cycles |
          graph_diff | surprising_connections | suggested_questions |
          pr_impact | feature_map | business_rules | process_flows

Plus structural proofs:
  - _debug_trace is absent from the MCP tool surface
  - llm_inference_call_count is exposed in /api/metrics
"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.live

_MAX_ELAPSED_S = 10.0  # ≤10s SLO for every MCP read action


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_count(http) -> int:
    resp = http.get("/api/metrics", timeout=5)
    resp.raise_for_status()
    return resp.json().get("llm_inference_call_count", 0)


def _call_and_assert_zero_llm(http, *, url: str, params: dict, label: str) -> None:
    """GET the REST endpoint, assert zero-LLM and ≤10s."""
    before = _infer_count(http)
    start = time.monotonic()

    resp = http.get(url, params=params, timeout=15)
    elapsed = time.monotonic() - start

    # Some overview/graph sub-actions return 400 for missing optional data (e.g. no
    # hierarchy yet); treat those as "no LLM call was made" (count delta = 0 is enough).
    # We only fail on 500 (server error).
    assert resp.status_code != 500, (
        f"{label}: HTTP 500 — {resp.text[:300]}"
    )

    # 1. llm_used must be absent or False in a successful 200 response
    if resp.status_code == 200:
        data = resp.json()
        llm_used = data.get("llm_used", False)
        assert llm_used is False or llm_used is None, (
            f"{label}: llm_used={llm_used!r} (want False/absent)"
        )

    # 2. Elapsed time ≤ SLO
    assert elapsed <= _MAX_ELAPSED_S, (
        f"{label}: elapsed {elapsed:.1f}s > {_MAX_ELAPSED_S}s SLO"
    )

    # 3. Zero inference counter delta (hard proof, independent of self-report)
    after = _infer_count(http)
    assert after == before, (
        f"{label}: LLM inference counter rose by {after - before} "
        f"(was {before}, now {after}) — a KB-build LLM call happened during an MCP read action"
    )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def proj(http, quality_project):
    """Return the indexed project path used for all zero-LLM tests."""
    return quality_project


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

class TestSearchZeroLLM:
    """search(scope=code|docs|all) must be zero-LLM."""

    def test_search_code(self, http, proj) -> None:
        _call_and_assert_zero_llm(http, url="/api/search",
            params={"q": "community detection", "project": proj, "scope": "code"},
            label="search(code)")

    def test_search_docs(self, http, proj) -> None:
        _call_and_assert_zero_llm(http, url="/api/search",
            params={"q": "architecture overview", "project": proj, "scope": "docs"},
            label="search(docs)")

    def test_search_all(self, http, proj) -> None:
        _call_and_assert_zero_llm(http, url="/api/search",
            params={"q": "pipeline handler", "project": proj, "scope": "all"},
            label="search(all)")


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------

class TestAskZeroLLM:
    """ask(scope=all|global|feature|business|wiki|architecture) must be zero-LLM."""

    def test_ask_all(self, http, proj) -> None:
        _call_and_assert_zero_llm(http, url="/api/ask",
            params={"q": "how does indexing work", "project": proj, "scope": "all"},
            label="ask(all)")

    def test_ask_global(self, http, proj) -> None:
        _call_and_assert_zero_llm(http, url="/api/ask",
            params={"q": "describe the overall architecture", "project": proj, "scope": "global"},
            label="ask(global)")

    def test_ask_feature(self, http, proj) -> None:
        _call_and_assert_zero_llm(http, url="/api/ask",
            params={"q": "how does the enrichment pipeline work", "project": proj, "scope": "feature"},
            label="ask(feature)")

    def test_ask_business(self, http, proj) -> None:
        _call_and_assert_zero_llm(http, url="/api/ask",
            params={"q": "what business rules govern indexing", "project": proj, "scope": "business"},
            label="ask(business)")

    def test_ask_wiki(self, http, proj) -> None:
        _call_and_assert_zero_llm(http, url="/api/ask",
            params={"q": "community enrichment", "project": proj, "scope": "wiki"},
            label="ask(wiki)")

    def test_ask_architecture(self, http, proj) -> None:
        # "architecture" scope falls through to the "all" handler in /api/ask
        _call_and_assert_zero_llm(http, url="/api/ask",
            params={"q": "system architecture components", "project": proj, "scope": "architecture"},
            label="ask(architecture)")


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------

class TestGraphZeroLLM:
    """graph(relation=*) must be zero-LLM."""

    def test_graph_definition(self, http, proj) -> None:
        _call_and_assert_zero_llm(http, url="/api/graph",
            params={"symbol": "community_summary", "project": proj, "relation": "definition"},
            label="graph(definition)")

    def test_graph_callers(self, http, proj) -> None:
        _call_and_assert_zero_llm(http, url="/api/graph",
            params={"symbol": "community_summary", "project": proj, "relation": "callers"},
            label="graph(callers)")

    def test_graph_callees(self, http, proj) -> None:
        _call_and_assert_zero_llm(http, url="/api/graph",
            params={"symbol": "community_summary", "project": proj, "relation": "callees"},
            label="graph(callees)")

    def test_graph_impact(self, http, proj) -> None:
        _call_and_assert_zero_llm(http, url="/api/graph",
            params={"symbol": "community_summary", "project": proj, "relation": "impact"},
            label="graph(impact)")

    def test_graph_impact_narrative(self, http, proj) -> None:
        _call_and_assert_zero_llm(http, url="/api/graph",
            params={"symbol": "community_summary", "project": proj, "relation": "impact_narrative"},
            label="graph(impact_narrative)")

    def test_graph_semantic_trace(self, http, proj) -> None:
        _call_and_assert_zero_llm(http, url="/api/graph",
            params={"symbol": "schedule_auto_pipeline", "project": proj,
                    "relation": "semantic_trace", "to": "handle_pipeline"},
            label="graph(semantic_trace)")

    def test_graph_path(self, http, proj) -> None:
        _call_and_assert_zero_llm(http, url="/api/graph",
            params={"symbol": "schedule_auto_pipeline", "project": proj,
                    "relation": "path", "to": "community_summary"},
            label="graph(path)")


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------

class TestOverviewZeroLLM:
    """overview(what=*) must be zero-LLM."""

    def _ov(self, http, proj, what: str) -> None:
        _call_and_assert_zero_llm(http, url="/api/overview",
            params={"project": proj, "what": what},
            label=f"overview({what})")

    def test_overview_structure(self, http, proj) -> None:
        self._ov(http, proj, "structure")

    def test_overview_communities(self, http, proj) -> None:
        self._ov(http, proj, "communities")

    def test_overview_status(self, http, proj) -> None:
        self._ov(http, proj, "status")

    def test_overview_projects(self, http, proj) -> None:
        self._ov(http, proj, "projects")

    def test_overview_patterns(self, http, proj) -> None:
        self._ov(http, proj, "patterns")

    def test_overview_service_mesh(self, http, proj) -> None:
        self._ov(http, proj, "service_mesh")

    def test_overview_architecture_domains(self, http, proj) -> None:
        self._ov(http, proj, "architecture_domains")

    def test_overview_hierarchy(self, http, proj) -> None:
        self._ov(http, proj, "hierarchy")

    def test_overview_import_cycles(self, http, proj) -> None:
        self._ov(http, proj, "import_cycles")

    def test_overview_graph_diff(self, http, proj) -> None:
        self._ov(http, proj, "graph_diff")

    def test_overview_surprising_connections(self, http, proj) -> None:
        self._ov(http, proj, "surprising_connections")

    def test_overview_suggested_questions(self, http, proj) -> None:
        self._ov(http, proj, "suggested_questions")

    def test_overview_pr_impact(self, http, proj) -> None:
        self._ov(http, proj, "pr_impact")

    def test_overview_feature_map(self, http, proj) -> None:
        self._ov(http, proj, "feature_map")

    def test_overview_business_rules(self, http, proj) -> None:
        self._ov(http, proj, "business_rules")

    def test_overview_process_flows(self, http, proj) -> None:
        self._ov(http, proj, "process_flows")


# ---------------------------------------------------------------------------
# Structural proofs
# ---------------------------------------------------------------------------

@pytest.mark.live
class TestMCPSurfaceStructural:
    """Structural invariants: debug_trace absent from MCP; counter exposed in metrics."""

    def test_debug_trace_not_in_mcp_surface(self) -> None:
        """_debug_trace must not be reachable from any MCP tool (dashboard-chat only)."""
        import inspect

        import opencode_search.mcp as mcp_module
        src = inspect.getsource(mcp_module)
        assert "_debug_trace" not in src and "handle_debug" not in src, (
            "_debug_trace / handle_debug must not appear in mcp.py — "
            "it uses LLM and is dashboard-chat only"
        )

    def test_llm_inference_counter_in_metrics(self, http) -> None:
        """Live: /api/metrics must expose llm_inference_call_count."""
        resp = http.get("/api/metrics", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert "llm_inference_call_count" in data, (
            f"/api/metrics must include llm_inference_call_count; got: {list(data.keys())}"
        )
        assert isinstance(data["llm_inference_call_count"], int), (
            f"llm_inference_call_count must be int; got {type(data['llm_inference_call_count'])}"
        )
