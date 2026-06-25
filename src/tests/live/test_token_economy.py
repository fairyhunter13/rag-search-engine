"""Token economy invariants (TE1–TE4, TE6; L2/L3 hierarchy removed in WS-B).

TE1  llm_token_stats() returns dotted-namespace keys (enrich/classify)
TE2  classify_communities_semantic makes 0 calls for narrated=0 tail rows (leak A gate)
TE4  _generate_narratives_batch returns {} safely when DeepSeek key absent
TE6  _BPRE_NARRATIVE_SYSTEM is a module-level string constant (stable prefix for caching)
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def _tail_store(tmp):
    from opencode_search.graph.community import label_community_structural
    from opencode_search.graph.store import GraphStore
    gs = GraphStore(tmp / "g.db")
    for i in range(5):
        gs.upsert_community(i + 1, level=1, title=f"TailMod{i}", summary="", member_count=2)
    gs.commit()
    for i in range(5):
        label_community_structural(gs, i + 1)
    return gs


def test_te1_token_stats_namespace_routing():
    """TE1: _accumulate_llm_tokens routes to dotted-namespace keys in llm_token_stats()."""
    from opencode_search.graph.llm import _accumulate_llm_tokens, llm_token_stats
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
    from opencode_search.graph.enrich import classify_communities_semantic
    from opencode_search.graph.llm import llm_token_stats
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
    from opencode_search.graph.llm import deepseek_key
    from opencode_search.kb.bpre import _generate_narratives_batch
    if deepseek_key():
        assert _generate_narratives_batch([]) == {}, "TE4: empty input must return {}"
        return
    procs = [(1, "Proc", '["svc"]', [("fn", "svc", "task", "")])]
    assert _generate_narratives_batch(procs) == {}, "TE4: must return {} without key"



def test_te6_bpre_narrative_system_constant():
    """TE6: _BPRE_NARRATIVE_SYSTEM is a stable module-level string (prefix-cache anchor)."""
    import opencode_search.kb.bpre as bpre_mod
    assert hasattr(bpre_mod, "_BPRE_NARRATIVE_SYSTEM"), (
        "TE6: _BPRE_NARRATIVE_SYSTEM missing — stable prefix constant deleted"
    )
    val = bpre_mod._BPRE_NARRATIVE_SYSTEM
    assert isinstance(val, str) and "JSON" in val, (
        f"TE6: constant must be a string containing 'JSON'; got: {val[:80]!r}"
    )
