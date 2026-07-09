"""Token economy invariants (TE1–TE4, TE6–TE8; L2/L3 hierarchy removed in WS-B).

TE1  llm_token_stats() returns dotted-namespace keys (enrich/classify)
TE2  classify_communities_semantic makes 0 calls for narrated=0 tail rows (leak A gate)
TE4  _generate_narratives_batch returns {} safely when DeepSeek key absent
TE6  _BPRE_NARRATIVE_SYSTEM is a module-level string constant (stable prefix for caching)
TE7  _llm_link_resolve (Tier-2 BPRE edge linking) accumulates under bpre_link.* — live,
     real DeepSeek call, proving the HR23 token-budget gap closed 2026-07-01 is observable
     end-to-end via llm_token_stats()/overview(what='metrics'), not just source-inspected.
TE8  _llm_link_resolve's real DeepSeek call obeys the HR18 SEA invariant (callee only ever
     drawn from the admitted service set) and the Tier-2 confidence floor (0.7) end-to-end
     against the live production edge-linker (kb/llm_escalation.py's parallel, never-wired
     escalate() helper was removed 2026-07-09 as confirmed dead code — this replaces its
     isolated unit-test coverage with a live proof against the real call site).
"""
from __future__ import annotations

import sqlite3

import pytest

pytestmark = pytest.mark.live


def _tail_store(tmp):
    from rag_search.graph.community import label_community_structural
    from rag_search.graph.store import GraphStore
    gs = GraphStore(tmp / "g.db")
    for i in range(5):
        gs.upsert_community(i + 1, level=1, title=f"TailMod{i}", summary="", member_count=2)
    gs.commit()
    for i in range(5):
        label_community_structural(gs, i + 1)
    return gs


def test_te1_token_stats_namespace_routing():
    """TE1: _accumulate_llm_tokens routes to dotted-namespace keys in llm_token_stats()."""
    from rag_search.graph.llm import _accumulate_llm_tokens, llm_token_stats
    _accumulate_llm_tokens(
        {"calls": 0, "completion_tokens": 0,
         "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0},
        "_te1_probe",
    )
    stats = llm_token_stats()
    assert "_te1_probe.calls" in stats, (
        f"TE1: _te1_probe.calls not in stats — namespace routing broken; keys: {list(stats)[:8]}"
    )


def test_te2_classify_skips_narrated_zero(safe_tmp_path):
    """TE2: classify_communities_semantic makes 0 LLM calls for narrated=0 tail (leak A)."""
    from rag_search.graph.enrich import classify_communities_semantic
    from rag_search.graph.llm import llm_token_stats
    gs = _tail_store(safe_tmp_path)
    try:
        before = llm_token_stats().get("classify.calls", 0)
        updated = classify_communities_semantic(gs)
        after = llm_token_stats().get("classify.calls", 0)
        assert after == before, f"TE2: classify made {after - before} calls on narrated=0 tail"
        assert updated == 0, f"TE2: classify returned {updated} type updates for tail-only store"
    finally:
        gs.close()



def test_te4_bpre_batch_no_key_safe():
    """TE4: _generate_narratives_batch returns {} when DeepSeek key absent."""
    from rag_search.graph.llm import deepseek_key
    from rag_search.kb.bpre import _generate_narratives_batch
    if deepseek_key():
        assert _generate_narratives_batch([]) == {}, "TE4: empty input must return {}"
        return
    procs = [(1, "Proc", '["svc"]', [("fn", "svc", "task", "")])]
    assert _generate_narratives_batch(procs) == {}, "TE4: must return {} without key"



def test_te6_bpre_narrative_system_constant():
    """TE6: _BPRE_NARRATIVE_SYSTEM is a stable module-level string (prefix-cache anchor)."""
    import rag_search.kb.bpre as bpre_mod
    assert hasattr(bpre_mod, "_BPRE_NARRATIVE_SYSTEM"), (
        "TE6: _BPRE_NARRATIVE_SYSTEM missing — stable prefix constant deleted"
    )
    val = bpre_mod._BPRE_NARRATIVE_SYSTEM
    assert isinstance(val, str) and "JSON" in val, (
        f"TE6: constant must be a string containing 'JSON'; got: {val[:80]!r}"
    )


def test_te7_llm_link_resolve_tokens_accounted_live():
    """TE7: _llm_link_resolve's real DeepSeek call increments bpre_link.calls (HR23, live)."""
    from rag_search.graph.llm import deepseek_key, llm_token_stats
    from rag_search.kb.bpre import _SCHEMA, _llm_link_resolve
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(_SCHEMA)
        before = llm_token_stats().get("bpre_link.calls", 0)
        items = [{"kind": "http", "caller": "checkout", "topic_or_route": "GET /cart/items"}]
        _llm_link_resolve(con, items, ["checkout", "cart", "inventory"])
        after = llm_token_stats().get("bpre_link.calls", 0)
        if deepseek_key():
            assert after == before + 1, (
                f"TE7: bpre_link.calls did not increment on a live _llm_link_resolve call "
                f"(before={before}, after={after}) — HR23 token accounting gap regressed"
            )
        else:
            assert after == before, "TE7: _llm_link_resolve must no-op without a key"
    finally:
        con.close()


def test_te8_llm_link_resolve_sea_invariant_live():
    """TE8: real _llm_link_resolve output obeys the HR18 SEA invariant + conf-0.7 floor."""
    from rag_search.graph.llm import deepseek_key
    from rag_search.kb.bpre import _SCHEMA, _llm_link_resolve
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(_SCHEMA)
        svcs = ["checkout", "cart", "inventory"]
        items = [
            {"kind": "http", "caller": "checkout", "topic_or_route": "GET /cart/items"},
            {"kind": "pubsub", "caller": "cart", "topic_or_route": "order.created"},
        ]
        _llm_link_resolve(con, items, svcs)
        rows = con.execute(
            "SELECT caller_service, callee_service, confidence FROM cross_service_edges "
            "WHERE kind LIKE '%_llm'"
        ).fetchall()
        if not deepseek_key():
            assert rows == [], "TE8: no _llm edges may be written without a DeepSeek key"
            return
        for caller, callee, confidence in rows:
            assert callee in svcs, f"TE8: SEA violated — callee {callee!r} not in {svcs}"
            assert caller != callee, f"TE8: verification gate violated — self-loop {caller!r}"
            assert confidence == 0.7, f"TE8: Tier-2 confidence must be 0.7, got {confidence}"
    finally:
        con.close()


def test_default_model_is_deepseek_v4_flash():
    """graph/llm.py must pin deepseek-v4-flash, not the deprecated deepseek-chat alias.

    Relocated from the removed test_llm_escalation_ladder.py (2026-07-09): this is a static
    check on graph/llm.py itself, independent of which caller invokes deepseek_extract.
    """
    import inspect

    from rag_search.graph import llm as llm_mod
    src = inspect.getsource(llm_mod)
    assert "deepseek-v4-flash" in src, "graph/llm.py must pin deepseek-v4-flash"
    assert "deepseek-chat" not in src or "deprecat" in src.lower(), (
        "deepseek-chat alias must not be used (deprecates 2026-07-24)"
    )
