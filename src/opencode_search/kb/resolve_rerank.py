"""Tier-1.75: GPU-local embed→cross-encoder rerank for BPRE residue resolution.

Uses the in-process CUDA cross-encoder (rerank_passages from query/search.py)
to disambiguate candidate targets for unresolved edges.  Free — no cloud call,
no new model load (the reranker is already warm from search operations).

SweRank (arXiv:2505.07849): retrieve-and-rerank beats Claude-3.5 localization
at ~60× lower cost.  SACL (arXiv:2506.20081): feed structural/type context,
not bare names, to the reranker.

Confidence: 0.8 RERANKED (below deterministic 0.9, above LLM 0.7).
Margin: if the top candidate's score does not exceed the margin, the item
        falls through to Tier-2 (conservative binding).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_DEFAULT_MARGIN = 0.05   # minimum score gap between top-1 and top-2 to bind


def rerank_candidates(
    query_context: str,
    candidates: list[str],
    *,
    margin: float = _DEFAULT_MARGIN,
) -> tuple[str | None, float]:
    """Score *candidates* against *query_context* with the CUDA cross-encoder.

    Args:
        query_context:  structural context string (caller signature + enclosing
                        symbol + import path — NOT bare function/service names).
        candidates:     list of candidate target descriptions (service names +
                        their structural context if available).
        margin:         minimum score gap (top1 - top2) to commit a binding.
                        Below the margin, returns (None, score) → falls to LLM.

    Returns:
        (best_candidate, score) where best_candidate is None if the margin is
        not met (ambiguous — propagate to Tier-2).
    """
    if not candidates:
        return None, 0.0
    try:
        from opencode_search.query.search import rerank_passages
    except Exception as exc:
        log.warning("resolve_rerank: rerank_passages unavailable: %s", exc)
        return None, 0.0

    scores = rerank_passages(query_context, candidates)
    if not scores:
        return None, 0.0

    ranked = sorted(zip(scores, candidates, strict=False), key=lambda x: x[0], reverse=True)
    top_score, top_cand = ranked[0]
    second_score = ranked[1][0] if len(ranked) >= 2 else 0.0

    if top_score - second_score >= margin:
        return top_cand, top_score
    # Ambiguous — do not commit; let Tier-2 decide
    return None, top_score


def rerank_residue(
    residue_items: list[dict],
    known_routes: dict[str, str],
    *,
    margin: float = _DEFAULT_MARGIN,
) -> tuple[list[dict], list[dict]]:
    """Attempt GPU-local resolution of residue items before the cloud LLM tier.

    Each item in *residue_items* must have keys:
        kind:            "http" | "grpc" | "pubsub"
        caller:          caller service name
        topic_or_route:  the unresolved route / topic / service FQN
        context:         (optional) structural context string for the reranker

    *known_routes* maps {route_string → service_name} — the admitted candidate
    set (SEA invariant: the LLM / reranker may only select, never author).

    Returns:
        (resolved, still_residue) — resolved items have "callee" + "confidence"
        0.8 added; still_residue falls through to Tier-2 LLM.
    """
    if not known_routes or not residue_items:
        return [], residue_items

    resolved: list[dict] = []
    remaining: list[dict] = []

    for item in residue_items:
        kind = item.get("kind", "")
        tor = item.get("topic_or_route", "")
        ctx = item.get("context", tor)

        if not tor:
            remaining.append(item)
            continue

        # Build candidate list from known_routes (same-kind endpoints)
        candidates = [r for r in known_routes if kind in r or kind == ""]
        if not candidates:
            remaining.append(item)
            continue

        best, _score = rerank_candidates(
            f"{ctx}\nQuery: {tor}",
            candidates,
            margin=margin,
        )
        if best is not None:
            callee = known_routes.get(best, best)
            resolved.append({**item, "callee": callee, "confidence": 0.8})
        else:
            remaining.append(item)

    return resolved, remaining
