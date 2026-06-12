"""Feature trace handler — ask(scope="feature").

Given a feature description (e.g. "add item to cart"), synthesizes a structured
design rationale document:
  - Entry points: which functions/endpoints handle this feature
  - Call chain: what calls what, annotated with WHY
  - Algorithm: natural language step-by-step description
  - Design rationale: architectural choices and reasons behind them

Algorithm:
  1. Semantic search to find the top-k most relevant symbols and code chunks
  2. For each top symbol found, retrieve 1-level callees from the graph
  3. Collect community summaries for context (the "why" layer)
  4. LLM synthesis: algorithm + design rationale from all gathered context
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_MAX_ENTRY_POINTS = 5
_MAX_CALLEES_PER_ENTRY = 8
_MAX_COMMUNITY_CONTEXT = 5


async def handle_ask_feature(
    query: str,
    project_path: str,
    top_k: int = 15,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Trace a feature end-to-end with algorithm and design rationale.

    Given a feature name or use-case description, returns:
      - entry_points: top functions/handlers that implement this feature
      - call_chain: ordered list of what calls what, with depth
      - algorithm: LLM-generated natural language algorithm (step-by-step)
      - design_rationale: WHY key design choices were made
      - involved_services: which services/components are involved and why
      - communities: relevant community summaries for context

    This is the "PC assembly" view: not just what the code does but why it
    was designed that way.

    use_cache=False: skip read and write; always synthesize fresh (required for
    judge-score quality tests so cache reuse never freezes reruns).
    """
    if use_cache:
        try:
            from opencode_search.handlers._answer_cache import load_answer, nearest_answer
            hit = load_answer(project_path, "feature", query)
            if hit is not None:
                return {**hit, "cached": True}
            near = nearest_answer(project_path, "feature", query)
            if near is not None:
                return {**near, "cached": "nearest"}
        except Exception:
            pass

    from opencode_search.config import get_project_graph_db_path
    from opencode_search.graph.storage import GraphStorage
    from opencode_search.handlers._kb_chat import _fetch_community_context
    from opencode_search.handlers._query import handle_search_code

    root = str(Path(project_path).expanduser().resolve())

    # ── Step 1: Search for relevant code ─────────────────────────────────────
    search_result = await handle_search_code(
        query=query,
        project_paths=[root],
        top_k=top_k,
        use_rerank=True,
    )
    raw_results = search_result.get("results", [])
    # When code search misses (relative paths, sparse index, etc.) we do NOT
    # early-return — the community graph + wiki often still carry the signal
    # to answer the question. The final dict guarantees an `answer` key so the
    # caller (/api/ask?scope=feature, tests) never sees the empty-key shape.

    # ── Step 2: Extract symbol names from search results ─────────────────────
    # Search results have qualified_name / symbol fields from the code chunks.
    entry_candidates: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for r in raw_results[:top_k]:
        sym = r.get("qualified_name") or r.get("symbol") or r.get("path", "")
        if sym and sym not in seen_symbols:
            seen_symbols.add(sym)
            entry_candidates.append({
                "symbol": sym,
                "file": r.get("path", ""),
                "kind": r.get("kind", ""),
                "language": r.get("language", ""),
                "score": round(r.get("score", 0.0), 3),
                "snippet": (r.get("content") or "")[:200],
            })

    # ── Step 3: Build call chain from graph ───────────────────────────────────
    community_ids_seen: set[int] = set()

    def _build_call_chain(project: str) -> tuple[list[dict], set[int]]:
        db = get_project_graph_db_path(project)
        if not Path(db).exists():
            return [], set()
        gs = GraphStorage(db)
        gs.open()
        chain: list[dict[str, Any]] = []
        comm_ids: set[int] = set()
        try:
            for candidate in entry_candidates[:_MAX_ENTRY_POINTS]:
                sym = candidate["symbol"]
                node = gs.get_node(sym)
                if node is None:
                    continue
                if node.community_id:
                    comm_ids.add(node.community_id)
                chain.append({
                    "symbol": node.qualified_name,
                    "name": node.name,
                    "file": node.file,
                    "kind": node.kind,
                    "depth": 0,
                    "community_id": node.community_id,
                    "docstring": (node.docstring or "")[:150],
                    "is_entry": True,
                })
                # Get callees (1 level deep)
                callees = gs.get_callees(node.id, depth=1)
                for callee in callees[:_MAX_CALLEES_PER_ENTRY]:
                    chain.append({
                        "symbol": callee.qualified_name,
                        "name": callee.name,
                        "file": callee.file,
                        "kind": callee.kind,
                        "depth": callee.depth,
                        "community_id": None,
                        "docstring": "",
                        "is_entry": False,
                    })
        finally:
            gs.close()
        return chain, comm_ids

    call_chain_raw, community_ids_seen = await asyncio.to_thread(_build_call_chain, root)

    # ── Step 4: Get community summaries for context ───────────────────────────
    community_contexts: list[dict[str, Any]] = []

    def _get_communities(project: str, comm_ids: set[int]) -> list[dict]:
        db = get_project_graph_db_path(project)
        if not Path(db).exists() or not comm_ids:
            return []
        gs = GraphStorage(db)
        gs.open()
        try:
            communities = gs.get_communities(min_node_count=2)
            result = []
            for c in communities:
                if c.id in comm_ids and c.title and c.summary:
                    result.append({
                        "id": c.id,
                        "title": c.title,
                        "summary": (c.summary or "")[:300],
                        "level": c.level,
                    })
            return result[:_MAX_COMMUNITY_CONTEXT]
        finally:
            gs.close()

    community_contexts = await asyncio.to_thread(_get_communities, root, community_ids_seen)

    # ── Step 4b: Query-based community fallback when call-chain context empty ──
    # If code search missed (no entry candidates) OR the call chain didn't
    # surface enriched communities, query the community graph directly with
    # the user's question text so we still have signal for the LLM to chew on.
    if not community_contexts:
        _, fallback_comms, _ = await _fetch_community_context(
            query, root, top_k=_MAX_COMMUNITY_CONTEXT, include_federation=False,
        )
        community_contexts = [
            {
                "id": idx,
                "title": c.get("title", ""),
                "summary": (c.get("summary") or "")[:300],
                "level": 0,
            }
            for idx, c in enumerate(fallback_comms[:_MAX_COMMUNITY_CONTEXT])
        ]

    # ── Step 5: Deterministic assembly — no LLM ──────────────────────────────
    # LLM synthesis has moved to the background pipeline warmer (_warm_answer_cache).
    # The read path returns structured index data only; llm_used is always False here.
    entry_text = "\n".join(
        f"- {e['symbol']} ({e['kind'] or 'fn'}) in {Path(e['file']).name}"
        for e in entry_candidates[:_MAX_ENTRY_POINTS]
    )
    chain_text = "\n".join(
        f"{'  ' * min(c['depth'], 3)}{c['symbol']} ({c['kind'] or 'fn'})"
        + (" [entry]" if c.get("is_entry") else f" [depth {c['depth']}]")
        for c in call_chain_raw[:20]
    )
    comm_text = "\n".join(
        f"[{c['title']}] {c['summary'][:250]}"
        for c in community_contexts
    )

    answer_parts = []
    if entry_text:
        answer_parts.append("Entry points:\n" + entry_text)
    if chain_text:
        answer_parts.append("Call chain:\n" + chain_text)
    if comm_text:
        answer_parts.append("Community context:\n" + comm_text)

    answer = "\n\n".join(answer_parts) if answer_parts else (
        f"No code or community context found for {query!r} in this project. "
        "Make sure it is indexed and enriched."
    )

    result = {
        "status": "ok",
        "query": query,
        "answer": answer,
        "entry_points": entry_candidates[:_MAX_ENTRY_POINTS],
        "call_chain": call_chain_raw,
        "algorithm": None,
        "design_rationale": None,
        "involved_services": [],
        "key_design_decisions": [],
        "communities": community_contexts,
        "llm_used": False,
    }
    if use_cache:
        try:
            from opencode_search.config import DEFAULT_DIMS, DEFAULT_EMBED_MODEL
            from opencode_search.embeddings import embed_query as _embed_query
            from opencode_search.handlers._answer_cache import save_answer
            emb = _embed_query(query, model=DEFAULT_EMBED_MODEL, dimensions=DEFAULT_DIMS)
            save_answer(project_path, "feature", query, result, embedding=emb)
        except Exception:
            pass
    return result
