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
    """
    from opencode_search.config import get_project_graph_db_path
    from opencode_search.enricher import create_llm_client
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

    # ── Step 5: LLM synthesis ─────────────────────────────────────────────────
    llm = await asyncio.to_thread(create_llm_client)
    if llm is None or not llm.is_available():
        msg = (
            "Feature trace unavailable: local KB LLM client could not be "
            "initialized. Ensure Ollama is running on GPU and qwen3-enrich "
            "is installed. Raw call chain (if any) is included below."
        )
        return {
            "status": "ok",
            "query": query,
            "answer": msg,
            "entry_points": entry_candidates[:_MAX_ENTRY_POINTS],
            "call_chain": call_chain_raw,
            "algorithm": None,
            "design_rationale": None,
            "involved_services": [],
            "key_design_decisions": [],
            "communities": community_contexts,
            "note": "LLM not available — raw call chain only",
        }

    # Build prompt context
    entry_lines = "\n".join(
        f"  - {e['symbol']} ({e['kind'] or 'function'}) in {Path(e['file']).name}"
        + (f"\n    {e['snippet'][:120]}" if e["snippet"] else "")
        for e in entry_candidates[:_MAX_ENTRY_POINTS]
    )
    chain_lines = "\n".join(
        f"  {'  ' * min(c['depth'], 3)}{c['symbol']} ({c['kind'] or 'fn'})"
        + (f" [depth={c['depth']}]" if c["depth"] > 0 else " [ENTRY]")
        for c in call_chain_raw[:20]
    )
    community_lines = "\n".join(
        f"  [{c['title']}]: {c['summary'][:200]}"
        for c in community_contexts
    ) or "  (no community summaries available — run build(action='pipeline') first)"

    synthesis: dict[str, Any] = {}
    try:
        import json
        raw_text = await asyncio.to_thread(
            llm.feature_trace,
            query,
            entry_lines or "(none found)",
            chain_lines or "(no call graph — run build(action=pipeline) first)",
            community_lines,
        )
        # Strip any accidental markdown fences
        raw_text = raw_text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        synthesis = json.loads(raw_text)
    except Exception as exc:
        log.debug("feature_trace LLM synthesis failed: %s", exc)
        synthesis = {"error": str(exc)}

    # Derive involved services: prefer LLM synthesis result; if missing, ask LLM
    # to infer from the call chain rather than using keyword-based path matching.
    involved_services: list[str] = synthesis.get("involved_services") or []
    if not involved_services and call_chain_raw:
        try:
            chain_files = list(dict.fromkeys(
                c["file"] for c in call_chain_raw if c.get("file")
            ))[:12]
            svc_prompt = (
                "From the following file paths in a call chain, list the distinct "
                "service or subsystem names (usually top-level directory names). "
                "Return a JSON array of strings, e.g. [\"auth\", \"payment\"]. "
                "Return [] if the paths are all in one service.\n\n"
                "Files:\n" + "\n".join(chain_files)
            )
            svc_raw = await asyncio.to_thread(
                llm.chat,
                [{"role": "user", "content": svc_prompt}],
                max_tokens=64,
            )
            import re as _re
            m = _re.search(r"\[.*?\]", (svc_raw or ""), _re.DOTALL)
            if m:
                involved_services = json.loads(m.group(0))
        except Exception:
            pass

    algorithm = synthesis.get("algorithm")
    design_rationale = synthesis.get("design_rationale")
    # Always surface a human-readable `answer` field. Callers (UI + tests) read
    # this first; keeping it populated even on partial/empty synthesis means
    # /api/ask?scope=feature never returns a body without an answer.
    answer_parts: list[str] = []
    if algorithm:
        answer_parts.append(str(algorithm).strip())
    if design_rationale:
        answer_parts.append("Why: " + str(design_rationale).strip())
    if not answer_parts:
        if entry_candidates or community_contexts:
            answer = (
                f"Feature trace for {query!r}: code search returned "
                f"{len(entry_candidates)} entry point candidate(s) and "
                f"{len(community_contexts)} relevant community summaries, "
                "but the local LLM did not produce a synthesis. See "
                "entry_points/communities for the raw context."
            )
        else:
            answer = (
                f"No code or community context found for {query!r} in this "
                "project. Make sure it is indexed (build action='pipeline') "
                "and enriched. Try rephrasing the query with a more specific "
                "function or feature name."
            )
        answer_parts = [answer]

    return {
        "status": "ok",
        "query": query,
        "answer": "\n\n".join(answer_parts),
        "entry_points": entry_candidates[:_MAX_ENTRY_POINTS],
        "call_chain": call_chain_raw,
        "algorithm": algorithm,
        "design_rationale": design_rationale,
        "involved_services": involved_services,
        "key_design_decisions": synthesis.get("key_design_decisions", []),
        "communities": community_contexts,
    }
