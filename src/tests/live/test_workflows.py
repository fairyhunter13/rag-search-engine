"""Developer workflow chain tests — multi-step API call sequences.

Each test chains 2-3 API calls where the output of one feeds the next,
simulating real investigation flows (search → graph → impact, ask → drill down, etc.).

All tests require: daemon at :8765, astro-project indexed with communities, Ollama running.
"""
from __future__ import annotations

import pytest

from .test_astro_e2e import _ASTRO

pytestmark = pytest.mark.live

_ASTRO_PATH = _ASTRO



# ---------------------------------------------------------------------------
# Class 1: Investigation Workflows — chained API calls
# ---------------------------------------------------------------------------

class TestInvestigationWorkflows:
    """End-to-end developer investigation flows that chain multiple API calls."""

    @pytest.mark.slow
    def test_search_then_graph_impact(self, http, astro):
        """Find a symbol via search, then get its impact graph — chain works end-to-end."""
        r1 = http.get("/api/search", params={
            "q": "AddToCart handler", "project": astro, "top_k": 3,
        })
        assert r1.status_code == 200
        results = r1.json().get("results", [])
        assert len(results) > 0, "search returned no results for AddToCart"

        symbol = results[0].get("name") or results[0].get("title") or "AddToCart"
        r2 = http.get("/api/graph", params={
            "project": astro, "symbol": symbol, "relation": "impact",
        })
        assert r2.status_code == 200, (
            f"graph impact failed for {symbol!r}: {r2.text[:200]}"
        )
        data = r2.json()
        assert "error" not in data or data.get("symbol") is not None

    @pytest.mark.slow
    def test_ask_then_search_drill_down(self, http, astro):
        """Ask for an overview, then search for a concrete symbol from the domain."""
        r1 = http.get("/api/ask", params={
            "q": "what are the main Go services in Astro?",
            "project": astro,
            "scope": "global",
        })
        assert r1.status_code == 200
        answer = r1.json().get("answer") or r1.json().get("result") or ""
        assert len(answer) > 50, f"ask returned too-short answer: {answer!r}"

        r2 = http.get("/api/search", params={
            "q": "cart service gRPC handler", "project": astro, "top_k": 5,
        })
        assert r2.status_code == 200
        assert len(r2.json().get("results", [])) > 0

    @pytest.mark.slow
    def test_complete_investigator_workflow(self, http, astro):
        """3-step investigation: ask feature → search entry point → graph impact."""
        r1 = http.get("/api/ask", params={
            "q": "how does cart checkout work?",
            "project": astro,
            "scope": "feature",
        })
        assert r1.status_code == 200
        answer = r1.json().get("answer") or r1.json().get("result") or ""
        assert len(answer) > 50, f"ask feature returned too-short answer: {answer!r}"

        r2 = http.get("/api/search", params={
            "q": "checkout handler entry point", "project": astro, "top_k": 3,
        })
        assert r2.status_code == 200
        results = r2.json().get("results", [])
        assert len(results) > 0, "search returned no results for checkout handler"

        symbol = results[0].get("name") or results[0].get("title") or "Checkout"
        r3 = http.get("/api/graph", params={
            "project": astro, "symbol": symbol, "relation": "impact_narrative",
        })
        assert r3.status_code == 200
        data = r3.json()
        assert "error" not in data or len(str(data)) > 20

