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
    if not raw_results:
        return {
            "status": "no_results",
            "query": query,
            "message": "No code found for this feature. Make sure the project is indexed.",
        }

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

    # ── Step 5: LLM synthesis ─────────────────────────────────────────────────
    llm = await asyncio.to_thread(create_llm_client)
    if llm is None or not llm.is_available():
        return {
            "status": "ok",
            "query": query,
            "entry_points": entry_candidates[:_MAX_ENTRY_POINTS],
            "call_chain": call_chain_raw,
            "algorithm": None,
            "design_rationale": None,
            "involved_services": [],
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

    # Infer involved services from file paths
    services_from_files: list[str] = []
    service_dirs: set[str] = set()
    for c in call_chain_raw:
        parts = Path(c["file"]).parts if c.get("file") else []
        # Look for service-like directory names
        for part in parts:
            _svc_kw = ["service", "handler", "controller", "api", "repo"]
            if any(keyword in part.lower() for keyword in _svc_kw):
                service_dirs.add(part)
    if service_dirs:
        services_from_files = sorted(service_dirs)

    return {
        "status": "ok",
        "query": query,
        "entry_points": entry_candidates[:_MAX_ENTRY_POINTS],
        "call_chain": call_chain_raw,
        "algorithm": synthesis.get("algorithm"),
        "design_rationale": synthesis.get("design_rationale"),
        "involved_services": synthesis.get("involved_services", services_from_files),
        "key_design_decisions": synthesis.get("key_design_decisions", []),
        "communities": community_contexts,
    }
