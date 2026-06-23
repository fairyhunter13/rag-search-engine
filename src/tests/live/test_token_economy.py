"""Token economy invariants (TE1–TE6, Phase 4A–F closed).

TE1  llm_token_stats() returns dotted-namespace keys (enrich/classify/l2)
TE2  classify_communities_semantic makes 0 calls for narrated=0 tail rows (leak A gate)
TE3  enrich_communities_l2_batch accumulates to l2.* namespace (leak B+C)
TE4  _generate_narratives_batch returns {} safely when DeepSeek key absent
TE5  L3 freshness guard: build_federation_hierarchy reuses on 2nd call < 1800s (leak F)
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


def test_te3_l2_batch_accumulates_l2_namespace(safe_tmp_path):
    """TE3: enrich_communities_l2_batch routes usage to l2.* namespace (leak B+C)."""
    from opencode_search.graph.enrich import enrich_communities_l2_batch
    from opencode_search.graph.llm import deepseek_key, llm_token_stats
    from opencode_search.graph.store import GraphStore
    if not deepseek_key():
        pytest.fail("TE3 requires OPENCODE_DEEPSEEK_API_KEY to verify l2.* accumulation")
    gs = GraphStore(safe_tmp_path / "l2.db")
    try:
        gs.upsert_community(1, level=1, title="Payment", summary="Handles payments.",
                            member_count=5, narrated=1)
        gs.upsert_community(100, level=2, title="", summary="", member_count=1)
        gs._con.execute("UPDATE communities SET parent_id=100 WHERE id=1")
        gs.commit()
        before = llm_token_stats().get("l2.calls", 0)
        enrich_communities_l2_batch(gs, [100])
        after = llm_token_stats().get("l2.calls", 0)
        assert after > before, f"TE3: l2.calls did not increase ({before}→{after})"
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


def test_te5_l3_freshness_guard_second_call(live_client):
    """TE5: build_federation_hierarchy returns cached count on 2nd call < 1800s (leak F)."""
    from opencode_search.core.registry import list_projects
    from opencode_search.daemon.federation import expand_federation
    from opencode_search.kb.federation_hierarchy import build_federation_hierarchy
    root_path = ""
    for p in list_projects():
        if p.enabled and len(expand_federation(p.path)) >= 2:
            root_path = p.path
            break
    if not root_path:
        pytest.fail("TE5 requires a registered federation root with ≥2 members")
    n1 = build_federation_hierarchy(root_path)
    n2 = build_federation_hierarchy(root_path)
    assert n2 == n1, f"TE5: freshness guard failed — 1st={n1}, 2nd={n2}"


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
