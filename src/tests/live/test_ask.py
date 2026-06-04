"""Live ask API tests — require daemon at :8765 with an indexed project."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live

_COMPONENT_WORDS = {"component", "service", "layer", "module", "architecture", "domain",
                    "handler", "api", "grpc", "http", "microservice", "package"}


def test_ask_global_returns_substantial_answer(http, project):
    """Global scope ask must return a non-trivial answer covering the whole system."""
    r = http.get("/api/ask", params={"q": "What is the overall architecture?", "project": project, "scope": "global"})
    assert r.status_code == 200, f"ask failed: {r.status_code} {r.text[:300]}"
    data = r.json()
    answer = data.get("answer", "") or data.get("summary", "") or str(data)
    assert len(answer) > 100, f"Global answer too short ({len(answer)} chars): {answer[:200]}"


def test_ask_architecture_mentions_components(http, project):
    """Architecture ask must mention structural components."""
    r = http.get("/api/ask", params={"q": "Describe the main architectural layers", "project": project})
    assert r.status_code == 200
    data = r.json()
    answer = (data.get("answer", "") or data.get("summary", "") or str(data)).lower()
    assert any(w in answer for w in _COMPONENT_WORDS), (
        f"Architecture answer must mention structural terms; got: {answer[:300]}"
    )


def test_ask_feature_scope_returns_structured_data(http, project):
    """Feature scope must return entry_points, call_chain, or algorithm — not just a bare string."""
    r = http.get("/api/ask", params={
        "q": "How does the main request flow work?",
        "project": project,
        "scope": "feature",
    })
    assert r.status_code == 200
    data = r.json()
    has_structure = any(k in data for k in (
        "entry_points", "call_chain", "algorithm", "design_rationale", "answer", "communities"
    ))
    assert has_structure, f"Feature scope returned no structured data; keys={list(data.keys())}"


def test_ask_returns_non_empty_for_concrete_question(http, project):
    """A concrete how-does-X-work question must return a non-empty answer."""
    r = http.get("/api/ask", params={"q": "How does search indexing work?", "project": project})
    assert r.status_code == 200
    data = r.json()
    answer = data.get("answer", "") or data.get("summary", "")
    communities = data.get("communities", [])
    assert len(answer) > 20 or len(communities) > 0, (
        f"ask returned empty result: {data}"
    )
