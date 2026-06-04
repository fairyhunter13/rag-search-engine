"""Live indexing tests — require daemon at :8765, real GPU, real Ollama.

Tests the full index pipeline: communities exist, enrichment % above threshold,
vector search returns results. Does NOT trigger a new pipeline run — verifies
state of the already-indexed project.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def test_indexed_project_has_communities(http, project):
    """An indexed project must have at least one community."""
    r = http.get("/api/overview", params={"project": project, "what": "communities"})
    assert r.status_code == 200, f"overview failed: {r.status_code} {r.text[:200]}"
    data = r.json()
    communities = data.get("communities", [])
    count = data.get("community_count", len(communities))
    assert count > 0, f"Indexed project has zero communities: {data}"


def test_enrichment_above_80pct(http, project):
    """At least 80% of communities must have a non-null title after enrichment."""
    r = http.get("/api/overview", params={"project": project, "what": "status"})
    assert r.status_code == 200, f"status failed: {r.status_code} {r.text[:200]}"
    data = r.json()

    total = data.get("community_count", 0) or data.get("total_communities", 0)
    enriched = data.get("enriched_count", 0) or data.get("communities_with_title", 0)

    if total == 0:
        r2 = http.get("/api/overview", params={"project": project, "what": "communities"})
        communities = r2.json().get("communities", [])
        total = len(communities)
        enriched = sum(1 for c in communities if c.get("title"))

    assert total > 0, f"Project has no communities to check enrichment: {data}"
    pct = enriched / total * 100
    assert pct >= 80, (
        f"Enrichment is only {pct:.0f}% ({enriched}/{total} communities have titles). "
        "Run build(action='enrich') to fix."
    )


def test_vector_search_returns_results(http, project):
    """Vector search must return at least one result for a generic code query."""
    r = http.get("/api/search", params={"q": "main entry point function", "project": project, "top_k": 5})
    assert r.status_code == 200, f"search failed: {r.status_code} {r.text[:200]}"
    results = r.json().get("results", [])
    assert len(results) > 0, (
        "Vector search returned no results — LanceDB index may be empty or corrupt"
    )


def test_graph_has_nodes(http, project):
    """The graph must have nodes after indexing (tree-sitter extraction worked)."""
    r = http.get("/api/overview", params={"project": project, "what": "structure"})
    assert r.status_code == 200
    data = r.json()
    graph_stats = data.get("graph_stats", {})
    node_count = (
        graph_stats.get("nodes", 0)
        or graph_stats.get("total_communities", 0)
        or data.get("node_count", 0)
        or data.get("graph", {}).get("nodes", 0)
    )
    assert node_count > 0, f"Graph has zero nodes — tree-sitter extraction may have failed: {graph_stats}"
