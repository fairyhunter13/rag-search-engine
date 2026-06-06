"""Edge case and error path tests for the opencode-search-engine HTTP API.

Tests cover:
- Missing required parameters → 400
- Invalid numeric parameter types → 400 (requires dashboard.py try-except fixes)
- Non-existent project paths → graceful error, never 5xx
- Boundary inputs: whitespace query, very long query, invalid scope/relation values

All tests require daemon at :8765. No LLM calls — all fast unless marked slow.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# Class 1: Missing required parameters
# ---------------------------------------------------------------------------

class TestMissingRequiredParams:
    """Every required param missing must return 400, never 5xx."""

    def test_search_without_query_returns_400(self, http, project):
        r = http.get("/api/search", params={"project": project})
        assert r.status_code == 400, (
            f"search without q= should be 400; got {r.status_code}: {r.text[:200]}"
        )
        assert "error" in r.json()

    def test_ask_without_project_returns_400(self, http):
        r = http.get("/api/ask", params={"q": "what is this?"})
        assert r.status_code == 400, (
            f"ask without project= should be 400; got {r.status_code}: {r.text[:200]}"
        )
        assert "error" in r.json()

    def test_graph_without_symbol_returns_400(self, http, project):
        r = http.get("/api/graph", params={"project": project})
        assert r.status_code == 400, (
            f"graph without symbol= should be 400; got {r.status_code}: {r.text[:200]}"
        )
        assert "error" in r.json()

    def test_chat_stream_without_query_returns_400(self, http, project):
        r = http.post("/api/chat_stream", json={"project": project})
        assert r.status_code == 400, (
            f"chat_stream without query key should be 400; got {r.status_code}: {r.text[:200]}"
        )

    def test_federation_add_without_member_returns_400(self, http, project):
        r = http.get("/api/federation", params={"project": project, "action": "add"})
        assert r.status_code == 400, (
            f"federation add without member= should be 400; got {r.status_code}: {r.text[:200]}"
        )
        assert "error" in r.json()


# ---------------------------------------------------------------------------
# Class 2: Invalid parameter types (numeric params with non-numeric input)
# ---------------------------------------------------------------------------

class TestInvalidParamTypes:
    """Numeric params with non-numeric strings must return 400, not 500."""

    def test_graph_non_integer_depth_returns_400(self, http, project):
        r = http.get("/api/graph", params={
            "project": project, "symbol": "main", "depth": "abc",
        })
        assert r.status_code == 400, (
            f"graph with depth=abc should be 400; got {r.status_code}: {r.text[:200]}"
        )
        assert "error" in r.json()

    def test_communities_non_integer_top_k_returns_400(self, http, project):
        r = http.get("/api/communities", params={"project": project, "top_k": "not_a_number"})
        assert r.status_code == 400, (
            f"communities with top_k=not_a_number should be 400; got {r.status_code}: {r.text[:200]}"
        )
        assert "error" in r.json()

    def test_dedup_non_float_threshold_returns_400(self, http, project):
        r = http.get("/api/dedup", params={"project": project, "threshold": "invalid"})
        assert r.status_code == 400, (
            f"dedup with threshold=invalid should be 400; got {r.status_code}: {r.text[:200]}"
        )
        assert "error" in r.json()

    def test_graph_export_non_integer_max_nodes_returns_400(self, http, project):
        r = http.get("/api/graph_export", params={"project": project, "max_nodes": "huge"})
        assert r.status_code == 400, (
            f"graph_export with max_nodes=huge should be 400; got {r.status_code}: {r.text[:200]}"
        )
        assert "error" in r.json()

    def test_search_negative_top_k_handled_gracefully(self, http, project):
        """Negative top_k: must return 200 or 400 — never 500."""
        r = http.get("/api/search", params={"q": "test", "project": project, "top_k": "-1"})
        assert r.status_code in (200, 400), (
            f"search with top_k=-1 caused {r.status_code}: {r.text[:200]}"
        )


# ---------------------------------------------------------------------------
# Class 3: Non-existent project paths
# ---------------------------------------------------------------------------

class TestInvalidProjectPaths:
    """Non-existent or empty project params must never return 5xx."""

    def test_search_nonexistent_project_no_500(self, http):
        r = http.get("/api/search", params={"q": "test", "project": "/nonexistent/path/xyz"})
        assert r.status_code < 500, (
            f"search with nonexistent project caused {r.status_code}: {r.text[:200]}"
        )

    def test_ask_nonexistent_project_no_500(self, http):
        r = http.get("/api/ask", params={"q": "what is this?", "project": "/no/such/project"})
        assert r.status_code < 500, (
            f"ask with nonexistent project caused {r.status_code}: {r.text[:200]}"
        )

    def test_overview_nonexistent_project_no_500(self, http):
        r = http.get("/api/overview", params={"project": "/no/such/project", "what": "structure"})
        assert r.status_code < 500, (
            f"overview with nonexistent project caused {r.status_code}: {r.text[:200]}"
        )

    def test_graph_nonexistent_project_no_500(self, http):
        r = http.get("/api/graph", params={"project": "/no/such", "symbol": "main"})
        assert r.status_code < 500, (
            f"graph with nonexistent project caused {r.status_code}: {r.text[:200]}"
        )

    def test_overview_empty_project_param_returns_400(self, http):
        r = http.get("/api/overview", params={"project": "", "what": "structure"})
        assert r.status_code == 400, (
            f"overview with empty project= should be 400; got {r.status_code}: {r.text[:200]}"
        )
        assert "error" in r.json()


# ---------------------------------------------------------------------------
# Class 4: Boundary input values
# ---------------------------------------------------------------------------

class TestBoundaryInputs:
    """Whitespace queries, very long queries, invalid enum values — must not crash."""

    def test_search_whitespace_only_query_graceful(self, http, project):
        """Whitespace-only q must return 200+empty or 400 — never 500."""
        r = http.get("/api/search", params={"q": "   ", "project": project})
        assert r.status_code in (200, 400), (
            f"whitespace-only query caused {r.status_code}: {r.text[:200]}"
        )
        if r.status_code == 200:
            assert isinstance(r.json().get("results", []), list)

    def test_search_very_long_query_no_500(self, http, project):
        """~1200 char query must not crash the server."""
        long_q = "architecture service function " * 40  # ~1200 chars
        r = http.get("/api/search", params={"q": long_q, "project": project})
        assert r.status_code < 500, (
            f"very long query caused {r.status_code}: {r.text[:200]}"
        )

    def test_ask_invalid_scope_no_500(self, http, project):
        """Unknown scope value must fall back gracefully — never 500."""
        r = http.get("/api/ask", params={
            "q": "what is this?", "project": project, "scope": "invalid_scope_xyz",
        })
        assert r.status_code < 500, (
            f"invalid scope caused {r.status_code}: {r.text[:200]}"
        )

    def test_graph_invalid_relation_returns_400(self, http, project):
        """Unknown relation value must return 400 (already validated in API)."""
        r = http.get("/api/graph", params={
            "project": project, "symbol": "main", "relation": "invalid_xyz",
        })
        assert r.status_code == 400, (
            f"invalid relation should be 400; got {r.status_code}: {r.text[:200]}"
        )
        assert "error" in r.json()

    @pytest.mark.slow
    def test_chat_stream_empty_query_returns_400_or_error_event(self, http, project):
        """Empty query string must return 400 or a proper error — must not hang."""
        r = http.post("/api/chat_stream", json={"project": project, "query": ""})
        assert r.status_code in (200, 400), (
            f"empty query caused {r.status_code}: {r.text[:200]}"
        )
        if r.status_code == 400:
            assert "error" in r.json()
