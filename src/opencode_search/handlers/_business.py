"""Business Knowledge Graph handlers.

Provides semantic views over the Leiden community graph:
  - feature_map:      all communities grouped by semantic_type
  - business_rules:   communities classified as business_rule
  - process_flows:    communities classified as business_process
  - ask_business:     LLM synthesis scoped to business-typed communities
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

_BUSINESS_TYPES = {"feature", "business_process", "business_rule"}


def _open_graph(project_path: str) -> Any:
    from pathlib import Path

    from opencode_search.config import get_project_graph_db_path
    from opencode_search.graph.storage import GraphStorage
    db_path = get_project_graph_db_path(project_path)
    if not Path(db_path).exists():
        return None
    gs = GraphStorage(db_path)
    gs.open()
    return gs


def _community_to_dict(c: Any, *, include_summary: bool = True) -> dict[str, Any]:
    return {
        "id": c.id,
        "title": c.title or f"Community {c.id}",
        "summary": (c.summary or "")[:300] if include_summary else None,
        "node_count": c.node_count,
        "level": c.level,
        "semantic_type": c.semantic_type,
        "key_entry_points": c.key_entry_points[:5],
    }


async def handle_feature_map(project_path: str, include_federation: bool = False) -> dict[str, Any]:
    """Return all communities grouped by semantic_type — the business knowledge map.

    Result shape:
    {
      "feature_map": {
        "feature": [...],
        "business_process": [...],
        "business_rule": [...],
        "data_model": [...],
        "api_boundary": [...],
        "infrastructure": [...],
        "utility": [...],
      },
      "summary": {"total": N, "classified": M, "by_type": {...}},
      "unclassified": N
    }
    """
    from opencode_search.config import load_registry
    from opencode_search.graph.storage import _SEMANTIC_TYPES

    registry = load_registry()
    paths = [project_path]
    if include_federation:
        from opencode_search.handlers._federation import _expand_with_federation
        paths = _expand_with_federation([project_path], registry)

    def _run(path: str) -> dict[str, Any]:
        gs = _open_graph(path)
        if gs is None:
            return {}
        try:
            counts = gs.get_semantic_type_counts()
            result: dict[str, list] = {t: [] for t in _SEMANTIC_TYPES}
            for stype in _SEMANTIC_TYPES:
                comms = gs.get_communities_by_semantic_type(stype)
                result[stype] = [_community_to_dict(c) for c in comms]
            all_comms = gs.get_communities(min_node_count=2)
            unclassified = sum(1 for c in all_comms if not c.semantic_type)
            return {
                "feature_map": result,
                "summary": {
                    "total": len(all_comms),
                    "classified": len(all_comms) - unclassified,
                    "by_type": counts,
                },
                "unclassified": unclassified,
                "project_path": path,
            }
        finally:
            gs.close()

    if len(paths) == 1:
        return await asyncio.to_thread(_run, paths[0])

    results = await asyncio.gather(*[asyncio.to_thread(_run, p) for p in paths])
    merged: dict[str, list] = {t: [] for t in _SEMANTIC_TYPES}
    total = classified = unclassified = 0
    by_type: dict[str, int] = {}
    for r in results:
        for t in _SEMANTIC_TYPES:
            merged[t].extend(r.get("feature_map", {}).get(t, []))
        total += r.get("summary", {}).get("total", 0)
        classified += r.get("summary", {}).get("classified", 0)
        unclassified += r.get("unclassified", 0)
        for k, v in r.get("summary", {}).get("by_type", {}).items():
            by_type[k] = by_type.get(k, 0) + v
    return {
        "feature_map": merged,
        "summary": {"total": total, "classified": classified, "by_type": by_type},
        "unclassified": unclassified,
        "projects": len(paths),
    }


async def handle_business_rules(project_path: str) -> dict[str, Any]:
    """Return communities classified as business_rule — constraints, policies, validations."""
    def _run() -> dict[str, Any]:
        gs = _open_graph(project_path)
        if gs is None:
            return {"error": "Project not indexed or graph not built"}
        try:
            comms = gs.get_communities_by_semantic_type("business_rule")
            return {
                "business_rules": [_community_to_dict(c, include_summary=True) for c in comms],
                "total": len(comms),
                "project_path": project_path,
            }
        finally:
            gs.close()
    return await asyncio.to_thread(_run)


async def handle_process_flows(project_path: str) -> dict[str, Any]:
    """Return communities classified as business_process — workflows, flows, sequences."""
    def _run() -> dict[str, Any]:
        gs = _open_graph(project_path)
        if gs is None:
            return {"error": "Project not indexed or graph not built"}
        try:
            comms = gs.get_communities_by_semantic_type("business_process")
            return {
                "process_flows": [_community_to_dict(c, include_summary=True) for c in comms],
                "total": len(comms),
                "project_path": project_path,
            }
        finally:
            gs.close()
    return await asyncio.to_thread(_run)


async def handle_ask_business(
    query: str,
    project_path: str,
    top_k: int = 10,
    include_federation: bool = False,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Answer business-domain questions using only business-typed communities.

    Filters the community graph to feature|business_process|business_rule
    communities and runs LLM synthesis over them. Best for:
    - 'What business rules govern checkout?'
    - 'Which features use the loyalty system?'
    - 'What workflows are involved in order fulfillment?'

    use_cache=False: skip read and write; always synthesize fresh.
    """
    if use_cache:
        try:
            from opencode_search.handlers._answer_cache import load_answer, nearest_answer
            hit = load_answer(project_path, "business", query)
            if hit is not None:
                return {**hit, "cached": True}
            near = nearest_answer(project_path, "business", query)
            if near is not None:
                return {**near, "cached": "nearest"}
        except Exception:
            pass

    from opencode_search.config import load_registry

    registry = load_registry()
    paths = [project_path]
    if include_federation:
        from opencode_search.handlers._federation import _expand_with_federation
        paths = _expand_with_federation([project_path], registry)

    query_lower = query.lower()

    def _gather_business_communities(path: str) -> list[dict[str, Any]]:
        gs = _open_graph(path)
        if gs is None:
            return []
        try:
            matches: list[dict[str, Any]] = []
            for btype in _BUSINESS_TYPES:
                for c in gs.get_communities_by_semantic_type(btype):
                    haystack = " ".join(filter(None, [c.title, c.summary])).lower()
                    tokens = [t for t in query_lower.split() if len(t) > 2]
                    score = sum(1 for t in tokens if t in haystack) / max(len(tokens), 1) if tokens else 0.5
                    if score > 0 or not tokens:
                        matches.append({
                            "title": c.title or f"Community {c.id}",
                            "summary": (c.summary or "")[:400],
                            "semantic_type": c.semantic_type,
                            "node_count": c.node_count,
                            "level": c.level,
                            "score": round(score, 4),
                            "project_path": path,
                        })
            matches.sort(key=lambda x: x["score"], reverse=True)
            return matches[:top_k]
        finally:
            gs.close()

    all_matches: list[dict[str, Any]] = []
    for path in paths:
        all_matches.extend(_gather_business_communities(path))
    all_matches.sort(key=lambda x: x["score"], reverse=True)
    top = all_matches[:top_k]

    if not top:
        return {
            "query": query,
            "answer": "No business-classified communities found. Run build(action='enrich') first to classify communities.",
            "communities": [],
        }

    # LLM synthesis over the top business communities
    context = "\n\n".join(
        f"[{c['semantic_type'].upper()}] {c['title']}\n{c['summary']}"
        for c in top
    )
    try:
        from opencode_search.enricher import create_kb_query_llm_client
        llm = create_kb_query_llm_client()
        prompt = (
            f"You are a business analyst. Based on these classified code communities, "
            f"answer this question:\n\n{query}\n\n"
            f"Business context from the codebase:\n{context[:3000]}\n\n"
            f"Answer concisely and specifically, referencing the relevant features/processes/rules."
        )
        answer = await asyncio.to_thread(
            llm.chat,
            [{"role": "user", "content": prompt}],
            max_tokens=500,
        )
    except Exception as exc:
        log.debug("business ask LLM failed: %s", exc)
        answer = f"Found {len(top)} relevant business communities. LLM synthesis unavailable: {exc}"

    result = {
        "query": query,
        "answer": answer,
        "communities": top,
        "total_business_communities": len(all_matches),
    }
    if use_cache:
        try:
            from opencode_search.config import DEFAULT_DIMS, DEFAULT_EMBED_MODEL
            from opencode_search.embeddings import embed_query as _embed_query
            from opencode_search.handlers._answer_cache import save_answer
            emb = _embed_query(query, model=DEFAULT_EMBED_MODEL, dimensions=DEFAULT_DIMS)
            save_answer(project_path, "business", query, result, embedding=emb)
        except Exception:
            pass
    return result
