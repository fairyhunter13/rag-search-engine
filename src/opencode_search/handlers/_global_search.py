"""Global query synthesis — GraphRAG-style map-reduce over all community summaries.

ask(query, scope="global") routes here.  Unlike vector search which retrieves
the top-k chunks, global synthesis considers ALL enriched communities and uses
a two-stage LLM map-reduce to produce a holistic answer.

Stages:
  1. RETRIEVE — fetch enriched communities, optionally filtered by text relevance
  2. MAP      — LLM extracts query-relevant information from each batch of ~8 communities
  3. REDUCE   — LLM synthesizes all MAP outputs into a final answer
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

_MAP_BATCH_SIZE = 12    # communities per MAP call
_MAX_COMMUNITIES = 60   # cap: 5 MAP batches × ~30s codex each = ~90s at semaphore(4)


def _score_community(title: str | None, summary: str | None, query_lower: str) -> float:
    """Simple token-overlap relevance score for pre-filtering communities."""
    haystack = " ".join(filter(None, [title, summary])).lower()
    if not haystack:
        return 0.0
    tokens = [t for t in query_lower.split() if len(t) > 2]
    if not tokens:
        return 1.0 if query_lower in haystack else 0.0
    return sum(1 for t in tokens if t in haystack) / len(tokens)


async def handle_global_synthesis(
    query: str,
    project_path: str,
    top_n: int = _MAX_COMMUNITIES,
    include_federation: bool = False,
    level: int = 1,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Answer a broad question by map-reducing over all community summaries.

    Analogous to GraphRAG's global search mode.  For broad queries ("what does
    this codebase do?") this provides a synthesized narrative rather than a
    ranked list of matching chunks.

    Args:
        query: The question to answer.
        project_path: Root project path (registry must have it indexed).
        top_n: Max communities to consider (default 200).
        include_federation: Include federated sub-repos.
        level: Community hierarchy level to query (1=micro, 2+=macro).
        use_cache: Serve from precomputed cache on hit; write to cache on miss.
            Set False for judge-score quality tests to ensure fresh synthesis.
    """
    if use_cache:
        try:
            from opencode_search.handlers._answer_cache import load_answer, nearest_answer
            cache_scope = f"global-L{level}"
            hit = load_answer(project_path, cache_scope, query)
            if hit is not None:
                return {**hit, "cached": True}
            near = nearest_answer(project_path, cache_scope, query)
            if near is not None:
                return {**near, "cached": "nearest"}
        except Exception:
            pass

    from opencode_search.config import load_registry
    from opencode_search.enricher import create_kb_query_llm_client
    from opencode_search.handlers._graph import _open_graph

    t0 = time.perf_counter()

    # ── Step 1: Collect enriched communities from all relevant projects ────────
    registry = load_registry()
    effective_paths = [project_path]
    if include_federation:
        from opencode_search.handlers._federation import _expand_with_federation
        effective_paths = _expand_with_federation([project_path], registry)

    query_lower = query.lower()
    all_communities: list[dict[str, Any]] = []

    def _fetch_communities(path: str) -> list[dict[str, Any]]:
        gs = _open_graph(path)
        if gs is None:
            return []
        try:
            comms = gs.get_communities(limit=top_n, min_node_count=2, order_by_size=True)
            enriched = [c for c in comms if c.title and c.summary]
            # Sort: fully enriched communities with title+summary first
            enriched.sort(key=lambda c: c.node_count, reverse=True)
            return [
                {
                    "id": c.id,
                    "title": c.title or "",
                    "summary": c.summary or "",
                    "node_count": c.node_count,
                    "project_path": path,
                    "score": _score_community(c.title, c.summary, query_lower),
                }
                for c in enriched[:top_n]
            ]
        except Exception as exc:
            log.warning("global_synthesis: cannot load communities for %s: %s", path, exc)
            return []
        finally:
            with contextlib.suppress(Exception):
                gs.close()

    import contextlib
    for path in effective_paths:
        all_communities.extend(await asyncio.to_thread(_fetch_communities, path))

    if not all_communities:
        return {
            "answer": "No enriched communities found. Run build(action='pipeline') first.",
            "sources": [],
            "community_count": 0,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000),
            "query": query,
        }

    # Sort by score (text-matching) descending, then by size
    all_communities.sort(key=lambda c: (c["score"], c["node_count"]), reverse=True)
    selected = all_communities[:top_n]

    log.info(
        "global_synthesis: %d enriched communities found, using top %d for query=%r",
        len(all_communities), len(selected), query[:60],
    )

    # ── Step 2: MAP — batch communities → LLM extracts relevant info ──────────
    llm = await asyncio.to_thread(create_kb_query_llm_client)
    batches: list[list[str]] = []
    for i in range(0, len(selected), _MAP_BATCH_SIZE):
        batch = selected[i:i + _MAP_BATCH_SIZE]
        summaries = [
            f"{c['title']}: {c['summary']}" for c in batch
        ]
        batches.append(summaries)

    # Limit concurrent Ollama calls to prevent CPU saturation (GPU handles tokens
    # but attention/tokenisation still saturates CPU cores with too many parallel requests)
    _llm_sem = asyncio.Semaphore(4)

    async def _map_batch(summaries: list[str]) -> str:
        async with _llm_sem:
            return await asyncio.to_thread(llm.map_query, query, summaries)

    map_tasks = [_map_batch(b) for b in batches]
    partial_answers = await asyncio.gather(*map_tasks, return_exceptions=True)
    valid_partials = [
        p for p in partial_answers
        if isinstance(p, str) and p.strip()
    ]

    if not valid_partials:
        return {
            "answer": "LLM synthesis unavailable or returned no results.",
            "sources": [c["id"] for c in selected[:10]],
            "community_count": len(selected),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000),
            "query": query,
        }

    # ── Step 3: REDUCE — synthesize all partial answers into final response ───
    final_answer = await asyncio.to_thread(llm.reduce_answers, query, valid_partials)

    # Collect source community IDs (those with score > 0 or first batch)
    sources = [
        c["id"] for c in selected
        if c["score"] > 0
    ][:20] or [c["id"] for c in selected[:10]]

    elapsed = round((time.perf_counter() - t0) * 1000)
    log.info(
        "global_synthesis: completed in %dms, %d map batches, %d partials → final answer",
        elapsed, len(batches), len(valid_partials),
    )

    result = {
        "answer": final_answer,
        "sources": sources,
        "community_count": len(selected),
        "map_batches": len(batches),
        "elapsed_ms": elapsed,
        "query": query,
        "scope": "global",
    }
    if use_cache:
        try:
            from opencode_search.config import DEFAULT_DIMS, DEFAULT_EMBED_MODEL
            from opencode_search.embeddings import embed_query as _embed_query
            from opencode_search.handlers._answer_cache import save_answer
            cache_scope = f"global-L{level}"
            emb = _embed_query(query, model=DEFAULT_EMBED_MODEL, dimensions=DEFAULT_DIMS)
            save_answer(project_path, cache_scope, query, result, embedding=emb)
        except Exception:
            pass
    return result
