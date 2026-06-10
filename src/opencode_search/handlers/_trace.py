"""Semantic trace — find entry and exit points by query, then trace the call chain with LLM narrative.

graph(relation="semantic_trace", symbol=<from_query>, to_symbol=<to_query>) routes here.
Combines vector search to locate symbols semantically + existing BFS path finding
+ LLM synthesis of a human-readable narrative.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


async def handle_semantic_trace(
    from_query: str,
    to_query: str,
    project_path: str,
    include_federation: bool = True,
) -> dict[str, Any]:
    """Trace a conceptual path through the codebase using natural language queries.

    1. Vector search to find the best-matching symbol for from_query (entry point)
    2. Vector search to find the best-matching symbol for to_query (exit point)
    3. BFS path finding from entry to exit via CALLS edges
    4. LLM generates a plain-English narrative describing the flow

    Example:
        handle_semantic_trace("HTTP request handler", "database write", project_path)
        → "The request enters via api.HandleRequest, passes through the service layer
           OrderService.ProcessOrder, validates payment via PaymentGateway.Charge,
           and finally writes to the database via Repository.Save."
    """
    import asyncio

    from opencode_search.enricher import create_kb_query_llm_client
    from opencode_search.handlers._graph import handle_trace_path
    from opencode_search.handlers._query import handle_search_code

    # Find from_symbol: search for the entry point
    from_results = await handle_search_code(
        query=from_query,
        project_paths=[project_path] if project_path else None,
        top_k=5,
        include_federation=include_federation,
    )
    from_candidates = from_results.get("results", [])

    # Find to_symbol: search for the exit point
    to_results = await handle_search_code(
        query=to_query,
        project_paths=[project_path] if project_path else None,
        top_k=5,
        include_federation=include_federation,
    )
    to_candidates = to_results.get("results", [])

    if not from_candidates:
        return {"error": f"No code found matching from_query: {from_query!r}"}
    if not to_candidates:
        return {"error": f"No code found matching to_query: {to_query!r}"}

    # Use the top candidate's file path to extract a symbol name
    def _extract_symbol(candidate: dict) -> str:
        path = candidate.get("path", "")
        content = candidate.get("content", "")
        # Try to extract function/class name from content
        import re
        m = re.search(r'(?:func|def|class|function)\s+(\w+)', content)
        if m:
            return m.group(1)
        # Fall back to file name without extension
        return path.rsplit("/", 1)[-1].rsplit(".", 1)[0] if path else "unknown"

    from_symbol = _extract_symbol(from_candidates[0])
    to_symbol = _extract_symbol(to_candidates[0])

    # Attempt path traversal
    path_data = await handle_trace_path(
        from_symbol=from_symbol,
        to_symbol=to_symbol,
        project_path=project_path,
    )

    path_nodes = path_data.get("path", [])
    found = path_data.get("found", False)

    # Generate narrative
    narrative = ""
    try:
        llm = await asyncio.to_thread(create_kb_query_llm_client)
        if found and path_nodes:
            narrative = await asyncio.to_thread(
                llm.trace_narrative,
                from_query,
                to_query,
                path_nodes,
            )
        else:
            narrative = (
                f"No direct call path found from '{from_symbol}' to '{to_symbol}'. "
                f"They may communicate indirectly or through different mechanisms. "
                f"Best candidates: from={from_symbol} ({from_candidates[0].get('path','')}), "
                f"to={to_symbol} ({to_candidates[0].get('path','')})."
            )
    except Exception as exc:
        log.debug("semantic_trace: LLM narrative failed: %s", exc)
        if found and path_nodes:
            steps = " → ".join(n.get("qualified_name") or n.get("name") or "?" for n in path_nodes[:8])
            narrative = f"Call chain ({len(path_nodes)} hops): {steps}"
        else:
            narrative = f"Path from '{from_symbol}' to '{to_symbol}' not found in call graph."

    return {
        "narrative": narrative,
        "path": path_nodes,
        "hops": len(path_nodes),
        "found": found,
        "from_symbol": from_symbol,
        "to_symbol": to_symbol,
        "from_query": from_query,
        "to_query": to_query,
        "from_candidate_path": from_candidates[0].get("path", "") if from_candidates else "",
        "to_candidate_path": to_candidates[0].get("path", "") if to_candidates else "",
    }
