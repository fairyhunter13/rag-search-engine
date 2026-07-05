"""Tier-2 LLM escalation — escalate() SEA invariant + cache stats + confidence.

All cloud tests are @slow (require DeepSeek key + network round-trip).
Gate/structure tests are fast (no network).
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live

_PREFIX = (
    "You are a microservice dependency resolver. "
    "Return a JSON array. Each element must have: kind, caller, topic_or_route, callee. "
    "callee MUST be one of the admitted targets. Return ONLY valid JSON."
)
_ITEMS = [{"kind": "grpc", "caller": "checkout-svc", "topic_or_route": "CartService"}]
_CANDS = ["cart-be", "order-be", "payment-be"]


def test_llm_cache_stats_structure():
    from rag_search.kb.llm_escalation import llm_cache_stats
    s = llm_cache_stats()
    assert isinstance(s, dict)
    for k in ("hits", "misses", "calls"):
        assert k in s, f"llm_cache_stats missing '{k}'"
    assert all(isinstance(v, int) for v in s.values())


def test_escalate_empty_items_returns_empty():
    from rag_search.kb.llm_escalation import escalate
    assert escalate([], stable_prefix=_PREFIX) == []



@pytest.mark.slow
def test_escalate_sea_invariant_callee_in_candidates():
    """All non-null callees must be ∈ the admitted candidate set (SEA invariant)."""
    from rag_search.graph.llm import deepseek_key
    assert deepseek_key(), "DeepSeek key required for Tier-2 test"
    from rag_search.kb.llm_escalation import escalate
    results = escalate(_ITEMS, stable_prefix=_PREFIX, candidates=_CANDS)
    for r in results:
        callee = r.get("callee")
        if callee is not None:
            assert callee in _CANDS, f"SEA violated: '{callee}' not in {_CANDS}"


@pytest.mark.slow
def test_escalate_confidence_at_most_0_7():
    """Tier-2 confidence must be ≤0.7 (LLM floor; deterministic tiers are higher)."""
    from rag_search.graph.llm import deepseek_key
    assert deepseek_key(), "DeepSeek key required"
    from rag_search.kb.llm_escalation import escalate
    for r in escalate(_ITEMS, stable_prefix=_PREFIX, candidates=_CANDS):
        if r.get("callee") is not None:
            assert r.get("confidence", 0.7) <= 0.7, f"Tier-2 conf > 0.7: {r}"


@pytest.mark.slow
def test_escalate_calls_counter_increments():
    """calls counter must increment; hits/misses count must be consistent."""
    from rag_search.graph.llm import deepseek_key
    assert deepseek_key(), "DeepSeek key required"
    from rag_search.kb.llm_escalation import escalate, llm_cache_stats
    before = llm_cache_stats()
    escalate(_ITEMS, stable_prefix=_PREFIX, candidates=_CANDS)
    after = llm_cache_stats()
    assert after["calls"] > before["calls"], "calls counter must increment"
    assert after["hits"] + after["misses"] >= after["calls"] - 1, (
        "hits + misses must track calls (off-by-one allowed for race)"
    )


@pytest.mark.slow
def test_escalate_cap_emits_warning(caplog):
    """Items > cap → truncation logged as WARNING."""
    from rag_search.graph.llm import deepseek_key
    assert deepseek_key(), "DeepSeek key required"
    import logging

    from rag_search.kb.llm_escalation import escalate
    big = [{"kind": "grpc", "caller": f"s{i}", "topic_or_route": "X"} for i in range(35)]
    with caplog.at_level(logging.WARNING, logger="rag_search.kb.llm_escalation"):
        escalate(big, stable_prefix=_PREFIX, candidates=_CANDS, cap=30)
    assert any("cap" in r.message.lower() for r in caplog.records), (
        "Truncation must emit WARNING"
    )


@pytest.mark.slow
def test_default_model_is_deepseek_v4_flash():
    """deepseek_extract uses deepseek-v4-flash (not the deprecated deepseek-chat alias)."""
    import inspect

    from rag_search.graph import llm as llm_mod
    src = inspect.getsource(llm_mod)
    assert "deepseek-v4-flash" in src, "graph/llm.py must pin deepseek-v4-flash"
    assert "deepseek-chat" not in src or "deprecat" in src.lower(), (
        "deepseek-chat alias must not be used (deprecates 2026-07-24)"
    )
