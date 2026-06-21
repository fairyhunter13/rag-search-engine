"""Reusable LLM-escalation helper for BPRE Tier-2/3 (DeepSeek V4 Flash).

Design principles
─────────────────
• SEA-style (arXiv:2408.04344): the LLM SELECT-s from a symbolically-admitted
  candidate set rather than authoring arbitrary edges (+8.1 F1).
• Stable-prefix caching: system prompt is byte-identical across batches →
  high DeepSeek automatic-cache hit rate ($0.0028/M vs $0.14/M miss).
• No strict-schema forcing (ExtractBench: strict forcing 86.9%→70.0%).
• No self-consistency sampling (arXiv:2511.00751: no significant gain).
• Capped + logged: any truncation emits a WARNING so coverage gaps are visible.
• Cache metrics exposed via /api/metrics (bpre.llm_cache_hits counter).
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

# Module-level cache-hit counter — read by /api/metrics
_llm_cache_stats: dict[str, int] = {"hits": 0, "misses": 0, "calls": 0}


def llm_cache_stats() -> dict[str, int]:
    """Return a copy of the module-level cache-hit counters."""
    return dict(_llm_cache_stats)


def escalate(
    items: list[dict],
    *,
    stable_prefix: str,
    candidates: list[str] | None = None,
    cap: int = 30,
    batch_chars: int = 150_000,
    timeout: int = 120,
    max_tokens: int = 4096,
) -> list[dict]:
    """Submit *items* to the LLM for SEA-style selection from *candidates*.

    Args:
        items:          list of dicts describing unresolved edges.
        stable_prefix:  system prompt (MUST be byte-identical across calls).
        candidates:     admitted target service/symbol set (SEA invariant).
                        If None, passes the raw items without candidate filtering.
        cap:            maximum items per call (excess emits WARNING + truncated).
        batch_chars:    approximate char budget for the dynamic tail.
        timeout:        HTTP timeout in seconds.
        max_tokens:     max output tokens.

    Returns:
        list of resolved dicts (model may return empty list or partial).
        Each resolved dict has the same keys as the input item plus "callee"
        and "confidence" (0.7 for Tier-2 SEA-select).
    """
    from opencode_search.graph.llm import deepseek_extract, deepseek_key
    if not deepseek_key():
        return []
    if not items:
        return []

    if len(items) > cap:
        log.warning("llm_escalation: cap %d hit (had %d items, truncating)", cap, len(items))
        items = items[:cap]

    cand_hint = ""
    if candidates:
        cand_hint = f"\nAdmitted targets (select ONLY from this list): {json.dumps(candidates)}"

    tail_parts = [f"- {json.dumps(it)}" for it in items]
    dynamic_tail = f"Items to resolve:{cand_hint}\n" + "\n".join(tail_parts)
    if len(dynamic_tail) > batch_chars:
        dynamic_tail = dynamic_tail[:batch_chars]

    _llm_cache_stats["calls"] += 1
    try:
        content, usage = deepseek_extract(
            stable_prefix, dynamic_tail, timeout=timeout, max_tokens=max_tokens,
        )
    except Exception as exc:
        log.warning("llm_escalation: DeepSeek call failed: %s", exc)
        return []

    if usage.get("prompt_cache_hit_tokens", 0) > 0:
        _llm_cache_stats["hits"] += 1
    else:
        _llm_cache_stats["misses"] += 1

    try:
        parsed = json.loads(content)
    except Exception:
        # Try to recover a JSON array embedded in text
        s, e = content.find("["), content.rfind("]")
        if s != -1 and e > s:
            try:
                parsed = json.loads(content[s:e + 1])
            except Exception:
                return []
        else:
            return []

    if isinstance(parsed, dict):
        # Some models wrap the array: {"results": [...]}
        for v in parsed.values():
            if isinstance(v, list):
                parsed = v
                break

    if not isinstance(parsed, list):
        return []

    cand_set = set(candidates) if candidates else None
    results: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        callee = item.get("callee")
        # SEA invariant: callee MUST be in the admitted candidate set
        if cand_set is not None and callee not in cand_set:
            continue
        item.setdefault("confidence", 0.7)
        results.append(item)
    return results
