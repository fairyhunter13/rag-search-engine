"""KB Chat handler — rich context assembly + query-tier LLM synthesis.

Answers complex questions like "list all features and their code" by combining
three context sources: vector code search, community graph, and wiki pages.

Two modes:
  quick        — single LLM call; fast (<5s); good for most questions
  comprehensive — MAP-REDUCE over community batches; slower but exhaustive;
                  ideal for "list everything" style queries
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from opencode_search.enricher import create_query_llm_client

log = logging.getLogger(__name__)

_MAP_BATCH_SIZE = 8
_MAX_COMMUNITIES = 60


async def handle_kb_chat(
    query: str,
    project_path: str,
    mode: str = "comprehensive",
    top_k: int = 20,
    include_federation: bool = False,
    conversation_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Answer a complex question by assembling full KB context + query-tier LLM.

    Args:
        query: Natural language question (supports long/complex queries).
        project_path: Indexed project path.
        mode: "quick" (single call) or "comprehensive" (MAP-REDUCE, more exhaustive).
        top_k: Max communities to include in context.
        include_federation: Whether to include federated sub-repos.
        conversation_history: Prior turns as [{role, content}, ...] for multi-turn chat.
    """
    t0 = time.perf_counter()

    # ── Step 1: Parallel context assembly ────────────────────────────────────
    code_task = _fetch_code_context(query, project_path, top_k=15)
    community_task = _fetch_community_context(query, project_path, top_k=top_k,
                                               include_federation=include_federation)
    wiki_task = _fetch_wiki_context(query, project_path, top_k=5)

    (code_ctx, code_sources, code_count), (comm_ctx, comm_list, comm_count), (wiki_ctx, wiki_count) = \
        await asyncio.gather(code_task, community_task, wiki_task)

    all_sources = list(dict.fromkeys(code_sources))  # deduplicated, preserving order

    # ── Step 2: LLM synthesis ─────────────────────────────────────────────────
    llm = await asyncio.to_thread(create_query_llm_client)
    if llm is None:
        return {
            "answer": "LLM unavailable. Check OPENCODE_QUERY_LLM_PROVIDER / OPENCODE_LLM_PROVIDER.",
            "sources": all_sources,
            "communities_used": comm_count,
            "code_results": code_count,
            "wiki_results": wiki_count,
            "mode": mode,
            "model": "none",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000),
        }

    model_name = getattr(llm, "model", type(llm).__name__)

    if mode == "comprehensive" and comm_count >= _MAP_BATCH_SIZE:
        answer = await _map_reduce_answer(query, comm_list, llm)
    else:
        answer = await _quick_answer(query, code_ctx, comm_ctx, wiki_ctx, llm, conversation_history)

    elapsed = round((time.perf_counter() - t0) * 1000)
    log.info("kb_chat: query=%r mode=%s communities=%d code=%d wiki=%d elapsed=%dms",
             query[:60], mode, comm_count, code_count, wiki_count, elapsed)

    return {
        "answer": answer,
        "sources": all_sources,
        "communities_used": comm_count,
        "code_results": code_count,
        "wiki_results": wiki_count,
        "mode": mode,
        "model": model_name,
        "elapsed_ms": elapsed,
    }


# ── Context fetchers ──────────────────────────────────────────────────────────

async def _fetch_code_context(
    query: str,
    project_path: str,
    top_k: int,
    extra_paths: list[str] | None = None,
) -> tuple[str, list[str], int]:
    """Return (context_text, source_paths, count). extra_paths adds federation members."""
    try:
        from opencode_search.handlers._query import handle_search_code
        all_paths = [project_path] + (extra_paths or [])
        result = await handle_search_code(
            query=query,
            project_paths=all_paths,
            top_k=top_k,
            use_rerank=False,
        )
        results = result.get("results", [])
        lines: list[str] = []
        paths: list[str] = []
        for r in results:
            path = r.get("path", "")
            snippet = (r.get("content") or r.get("snippet") or "").strip()[:200]
            score = r.get("score", 0.0)
            if path:
                paths.append(path)
                lines.append(f"{path} (score {score:.2f}): {snippet}")
        ctx = "\n".join(lines) if lines else ""
        return ctx, paths, len(results)
    except Exception as exc:
        log.debug("kb_chat: code context fetch failed: %s", exc)
        return "", [], 0


async def _fetch_community_context(
    query: str, project_path: str, top_k: int, include_federation: bool
) -> tuple[str, list[dict[str, Any]], int]:
    """Return (context_text, community_dicts, count)."""
    try:
        from opencode_search.handlers._graph import _open_graph

        query_lower = query.lower()
        tokens = [t for t in query_lower.split() if len(t) > 2]

        def _score(title: str | None, summary: str | None) -> float:
            hay = " ".join(filter(None, [title, summary])).lower()
            if not hay or not tokens:
                return 0.0
            return sum(1 for t in tokens if t in hay) / len(tokens)

        paths = [project_path]
        if include_federation:
            with contextlib.suppress(Exception):
                from opencode_search.config import load_registry
                from opencode_search.handlers._federation import _expand_with_federation
                paths = _expand_with_federation([project_path], load_registry())

        def _load(path: str) -> list[dict[str, Any]]:
            gs = _open_graph(path)
            if gs is None:
                return []
            try:
                comms = gs.get_communities(limit=_MAX_COMMUNITIES, min_node_count=2,
                                           order_by_size=True)
                enriched = [c for c in comms if c.title and c.summary]
                enriched.sort(key=lambda c: c.node_count, reverse=True)
                return [
                    {
                        "title": c.title,
                        "summary": c.summary or "",
                        "semantic_type": c.semantic_type or "utility",
                        "node_count": c.node_count,
                        "score": _score(c.title, c.summary),
                    }
                    for c in enriched
                ]
            finally:
                with contextlib.suppress(Exception):
                    gs.close()

        all_comms: list[dict[str, Any]] = []
        for comms in await asyncio.gather(*[asyncio.to_thread(_load, p) for p in paths]):
            all_comms.extend(comms)

        all_comms.sort(key=lambda c: (c["score"], c["node_count"]), reverse=True)
        selected = all_comms[:top_k]

        lines = [
            f"[{c['semantic_type']}] {c['title']}: {c['summary'][:300]}"
            for c in selected
        ]
        ctx = "\n".join(lines) if lines else ""
        return ctx, selected, len(selected)
    except Exception as exc:
        log.debug("kb_chat: community context fetch failed: %s", exc)
        return "", [], 0


async def _fetch_wiki_context(
    query: str, project_path: str, top_k: int
) -> tuple[str, int]:
    """Return (context_text, count)."""
    try:
        from opencode_search.handlers._wiki import handle_wiki_query
        result = await handle_wiki_query(query=query, project_path=project_path, top_k=top_k)
        pages = result.get("results", [])
        lines = [
            f"## {p.get('title', p.get('name', ''))}\n{(p.get('content') or p.get('excerpt', ''))[:400]}"
            for p in pages
        ]
        ctx = "\n\n".join(lines) if lines else ""
        return ctx, len(pages)
    except Exception as exc:
        log.debug("kb_chat: wiki context fetch failed: %s", exc)
        return "", 0


async def _fetch_hierarchy_communities(
    project_path: str, max_count: int = 30
) -> list[dict[str, Any]]:
    """Return top-level (level=max) hierarchy communities for structural breadth coverage.

    Vector similarity search finds semantically similar communities; this finds the
    high-level cluster summaries that represent the whole codebase structurally.
    Returns [] if no hierarchy has been built (max_level == 1).
    """
    def _load() -> list[dict[str, Any]]:
        from opencode_search.config import load_registry
        from opencode_search.graph.storage import GraphStorage
        registry = load_registry()
        entry = registry.get(project_path)
        if not entry:
            return []
        gs = GraphStorage(entry.db_path)
        max_lvl = gs.get_max_community_level()
        if max_lvl <= 1:
            return []
        comms = gs.get_communities(level=max_lvl, order_by_size=True, limit=max_count)
        return [
            {
                "id": c.id,
                "title": c.title or "",
                "summary": c.summary or "",
                "semantic_type": c.semantic_type or "utility",
                "node_count": c.node_count,
            }
            for c in comms
            if c.title and c.summary
        ]

    try:
        return await asyncio.to_thread(_load)
    except Exception as exc:
        log.debug("kb_chat: hierarchy communities fetch failed: %s", exc)
        return []


# ── LLM call strategies ────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a senior software architect. "
    "Answer exhaustively and factually using ONLY the provided context. "
    "For lists of features or functionalities, be complete and structured with code file references. "
    "Never fabricate code, files, or functionality not present in the context."
)


async def _quick_answer(
    query: str,
    code_ctx: str,
    comm_ctx: str,
    wiki_ctx: str,
    llm: Any,
    conversation_history: list[dict[str, Any]] | None = None,
) -> str:
    sections: list[str] = []
    if code_ctx:
        sections.append(f"[CODE LOCATIONS]\n{code_ctx}")
    if comm_ctx:
        sections.append(f"[ARCHITECTURE COMMUNITIES]\n{comm_ctx}")
    if wiki_ctx:
        sections.append(f"[WIKI KNOWLEDGE]\n{wiki_ctx}")

    if not sections:
        return "No indexed content found. Run build(action='pipeline') to index the project first."

    context = "\n\n".join(sections)
    system_content = f"{_SYSTEM_PROMPT}\n\nContext from the knowledge base:\n{context}"

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
    # Inject prior conversation turns (last 6 = 3 exchanges) for multi-turn chat
    for turn in (conversation_history or [])[-6:]:
        role = turn.get("role", "")
        content = turn.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": query})

    return await asyncio.to_thread(llm.chat, messages, max_tokens=2048)


async def _map_reduce_answer(
    query: str,
    communities: list[dict[str, Any]],
    llm: Any,
) -> str:
    """MAP-REDUCE over community batches — best for exhaustive "list everything" queries."""
    batches: list[list[str]] = []
    for i in range(0, len(communities), _MAP_BATCH_SIZE):
        batch = communities[i:i + _MAP_BATCH_SIZE]
        summaries = [
            f"[{c['semantic_type']}] {c['title']}: {c['summary']}"
            for c in batch
        ]
        batches.append(summaries)

    # Limit to 2 concurrent Ollama calls — prevents CPU saturation from
    # tokenization/attention overheads when many parallel requests compete.
    _sem = asyncio.Semaphore(2)

    async def _map_one(summaries: list[str]) -> str:
        async with _sem:
            return await asyncio.to_thread(llm.map_query, query, summaries)

    partial_answers = await asyncio.gather(*[_map_one(b) for b in batches], return_exceptions=True)
    valid = [p for p in partial_answers if isinstance(p, str) and p.strip()]

    if not valid:
        return "LLM synthesis returned no results."

    return await asyncio.to_thread(llm.reduce_answers, query, valid)
