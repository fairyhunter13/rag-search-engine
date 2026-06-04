"""Live search API tests — require daemon at :8765 with an indexed project."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live

_CODE_EXTENSIONS = {".go", ".py", ".java", ".ts", ".tsx", ".js", ".jsx", ".rs", ".kt", ".rb", ".cpp", ".c"}


def test_search_returns_file_paths(http, project):
    """Search results must include real source file paths."""
    r = http.get("/api/search", params={"q": "main handler function", "project": project, "top_k": 5})
    assert r.status_code == 200, f"Search failed: {r.status_code} {r.text[:200]}"
    data = r.json()
    results = data.get("results", [])
    assert len(results) > 0, f"No search results returned: {data}"
    paths = [res.get("file", res.get("path", "")) for res in results]
    code_files = [p for p in paths if any(p.endswith(ext) for ext in _CODE_EXTENSIONS)]
    assert len(code_files) >= 1, f"No code file paths in results; got: {paths}"


def test_search_top_k_respected(http, project):
    """Requesting top_k=3 must return at most 3 results."""
    r = http.get("/api/search", params={"q": "function definition", "project": project, "top_k": 3})
    assert r.status_code == 200
    results = r.json().get("results", [])
    assert len(results) <= 3, f"top_k=3 returned {len(results)} results"


def test_search_different_queries_differ(http, project):
    """Two semantically unrelated queries must return different top results."""
    r1 = http.get("/api/search", params={"q": "HTTP server route handler", "project": project, "top_k": 1})
    r2 = http.get("/api/search", params={"q": "database schema migration", "project": project, "top_k": 1})
    assert r1.status_code == 200 and r2.status_code == 200
    top1 = r1.json().get("results", [{}])[0].get("file", r1.json().get("results", [{}])[0].get("path", ""))
    top2 = r2.json().get("results", [{}])[0].get("file", r2.json().get("results", [{}])[0].get("path", ""))
    assert top1 != top2, (
        f"Different queries returned identical top result '{top1}' — "
        "embeddings may not be working correctly"
    )


def test_search_returns_scored_results(http, project):
    """Search results must have a score or distance field."""
    r = http.get("/api/search", params={"q": "error handling", "project": project, "top_k": 5})
    assert r.status_code == 200
    results = r.json().get("results", [])
    assert results, "No results"
    first = results[0]
    has_score = any(k in first for k in ("score", "distance", "similarity", "_distance"))
    assert has_score, f"Result missing score field; keys={list(first.keys())}"
