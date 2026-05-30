"""Graph MCP handlers: symbol lookup, call traversal, impact analysis."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opencode_search.config import get_project_graph_db_path

if TYPE_CHECKING:
    from opencode_search.graph.storage import GraphStorage

log = logging.getLogger(__name__)


def _open_graph(project_path: str) -> GraphStorage | None:
    from opencode_search.graph.storage import GraphStorage

    db_path = get_project_graph_db_path(project_path)
    if not Path(db_path).exists():
        return None
    gs = GraphStorage(db_path)
    gs.open()
    return gs


async def handle_get_symbol(name: str, project_path: str) -> dict[str, Any]:
    """Find a symbol by name or qualified_name. Returns definition + caller/callee counts."""
    import asyncio

    def _run() -> dict[str, Any]:
        gs = _open_graph(project_path)
        if gs is None:
            return {"error": "project not indexed or graph not built", "project_path": project_path}
        try:
            nodes = gs.get_nodes_by_name(name)
            if not nodes:
                return {"error": f"symbol '{name}' not found", "matches": []}
            results = []
            for n in nodes:
                callers = gs.get_callers(n.id, depth=1)
                callees = gs.get_callees(n.id, depth=1)
                results.append({
                    "id": n.id,
                    "name": n.name,
                    "qualified_name": n.qualified_name,
                    "kind": n.kind,
                    "file": n.file,
                    "start_line": n.start_line,
                    "end_line": n.end_line,
                    "language": n.language,
                    "signature": n.signature,
                    "docstring": n.docstring,
                    "community_id": n.community_id,
                    "intent": n.intent,
                    "caller_count": len(callers),
                    "callee_count": len(callees),
                })
            return {"matches": results, "count": len(results)}
        finally:
            gs.close()

    return await asyncio.to_thread(_run)


async def handle_get_callers(
    symbol: str,
    project_path: str,
    depth: int = 5,
) -> dict[str, Any]:
    """BFS upstream: who calls this symbol."""
    import asyncio

    def _run() -> dict[str, Any]:
        gs = _open_graph(project_path)
        if gs is None:
            return {"error": "graph not built", "callers": []}
        try:
            node = gs.get_node(symbol)
            if node is None:
                return {"error": f"symbol '{symbol}' not found", "callers": []}
            chain = gs.get_callers(node.id, depth=depth)
            return {
                "symbol": symbol,
                "node_id": node.id,
                "callers": [
                    {
                        "node_id": c.node_id,
                        "name": c.name,
                        "qualified_name": c.qualified_name,
                        "file": c.file,
                        "kind": c.kind,
                        "depth": c.depth,
                        "confidence": round(c.confidence, 3),
                    }
                    for c in chain
                ],
                "total": len(chain),
            }
        finally:
            gs.close()

    return await asyncio.to_thread(_run)


async def handle_get_callees(
    symbol: str,
    project_path: str,
    depth: int = 5,
) -> dict[str, Any]:
    """BFS downstream: what does this symbol call."""
    import asyncio

    def _run() -> dict[str, Any]:
        gs = _open_graph(project_path)
        if gs is None:
            return {"error": "graph not built", "callees": []}
        try:
            node = gs.get_node(symbol)
            if node is None:
                return {"error": f"symbol '{symbol}' not found", "callees": []}
            chain = gs.get_callees(node.id, depth=depth)
            return {
                "symbol": symbol,
                "node_id": node.id,
                "callees": [
                    {
                        "node_id": c.node_id,
                        "name": c.name,
                        "qualified_name": c.qualified_name,
                        "file": c.file,
                        "kind": c.kind,
                        "depth": c.depth,
                        "confidence": round(c.confidence, 3),
                    }
                    for c in chain
                ],
                "total": len(chain),
            }
        finally:
            gs.close()

    return await asyncio.to_thread(_run)


async def handle_trace_path(
    from_symbol: str,
    to_symbol: str,
    project_path: str,
) -> dict[str, Any]:
    """BFS shortest path between two symbols."""
    import asyncio

    def _run() -> dict[str, Any]:
        gs = _open_graph(project_path)
        if gs is None:
            return {"error": "graph not built", "path": []}
        try:
            from_node = gs.get_node(from_symbol)
            to_node = gs.get_node(to_symbol)
            if from_node is None:
                return {"error": f"symbol '{from_symbol}' not found", "path": []}
            if to_node is None:
                return {"error": f"symbol '{to_symbol}' not found", "path": []}
            node_ids = gs.trace_path(from_node.id, to_node.id)
            if node_ids is None:
                return {
                    "from": from_symbol, "to": to_symbol,
                    "path": [], "connected": False,
                }
            steps = []
            for nid in node_ids:
                n = gs.get_node_by_id(nid)
                steps.append({
                    "node_id": nid,
                    "name": n.name if n else nid,
                    "qualified_name": n.qualified_name if n else nid,
                    "file": n.file if n else "",
                    "kind": n.kind if n else "",
                })
            return {
                "from": from_symbol, "to": to_symbol,
                "path": steps,
                "hops": len(steps) - 1,
                "connected": True,
            }
        finally:
            gs.close()

    return await asyncio.to_thread(_run)


async def handle_detect_impact(
    symbol: str,
    project_path: str,
) -> dict[str, Any]:
    """Blast radius: everything that transitively calls this symbol."""
    import asyncio
    from collections import defaultdict

    def _run() -> dict[str, Any]:
        gs = _open_graph(project_path)
        if gs is None:
            return {"error": "graph not built", "callers_by_depth": {}}
        try:
            node = gs.get_node(symbol)
            if node is None:
                return {"error": f"symbol '{symbol}' not found", "callers_by_depth": {}}
            chain = gs.get_callers(node.id, depth=10)
            by_depth: dict[int, list[dict]] = defaultdict(list)
            for c in chain:
                by_depth[c.depth].append({
                    "node_id": c.node_id,
                    "name": c.name,
                    "qualified_name": c.qualified_name,
                    "file": c.file,
                    "kind": c.kind,
                    "confidence": round(c.confidence, 3),
                })
            return {
                "symbol": symbol,
                "node_id": node.id,
                "total_affected": len(chain),
                "callers_by_depth": {str(k): v for k, v in sorted(by_depth.items())},
            }
        finally:
            gs.close()

    return await asyncio.to_thread(_run)


async def handle_get_communities(
    project_path: str,
    top_k: int = 100,
) -> dict[str, Any]:
    """Return top Leiden communities for a project, ordered by size.

    Args:
        top_k: Maximum communities to return (default 100). Singleton communities
               (node_count == 1) are always excluded as they carry no structural
               information. Use a lower value on large projects to avoid timeouts.
    """
    import asyncio

    def _run() -> dict[str, Any]:
        gs = _open_graph(project_path)
        if gs is None:
            return {"communities": [], "total": 0, "error": "graph not built"}
        try:
            communities = gs.get_communities(
                limit=top_k,
                min_node_count=2,
                order_by_size=True,
            )
            result = []
            for c in communities:
                result.append({
                    "id": c.id,
                    "title": c.title,
                    "summary": c.summary,
                    "node_count": c.node_count,
                    "key_entry_points": c.key_entry_points,
                    "generated_at": c.generated_at,
                })
            return {"communities": result, "total": len(result)}
        finally:
            gs.close()

    return await asyncio.to_thread(_run)


async def handle_global_search(
    query: str,
    project_path: str,
    top_k: int = 10,
) -> dict[str, Any]:
    """Search across architectural knowledge: community summaries + wiki pages.

    Combines:
    - Community titles/summaries (fuzzy text match from graph DB)
    - Wiki pages (vector search via search_code filtered to wiki languages)

    Best for questions like 'which layer handles authentication?' or
    'where is the billing logic?'
    """
    import asyncio

    query_lower = query.lower()

    def _search_communities() -> list[dict[str, Any]]:
        gs = _open_graph(project_path)
        if gs is None:
            return []
        try:
            # Limit to top 500 meaningful communities (node_count >= 2) ordered by
            # size. This bounds Python-side text matching: on a 163k-community
            # project this goes from 163k rows to ~600 rows (a ~270x speedup).
            communities = gs.get_communities(
                limit=500, min_node_count=2, order_by_size=True
            )
            matches: list[dict[str, Any]] = []
            for c in communities:
                haystack = " ".join(filter(None, [c.title, c.summary])).lower()
                if not haystack:
                    continue
                tokens = [t for t in query_lower.split() if len(t) > 2]
                if not tokens:
                    score = 1.0 if query_lower in haystack else 0.0
                else:
                    score = sum(1 for t in tokens if t in haystack) / len(tokens)
                if score > 0:
                    matches.append({
                        "type": "community",
                        "id": c.id,
                        "title": c.title or f"Community {c.id}",
                        "summary": c.summary or "",
                        "node_count": c.node_count,
                        "key_entry_points": c.key_entry_points,
                        "score": round(score, 4),
                    })
            matches.sort(key=lambda x: x["score"] or 0.0, reverse=True)
            return matches[:top_k]
        finally:
            gs.close()

    from opencode_search.handlers._query import handle_search_code

    community_hits, wiki_result = await asyncio.gather(
        asyncio.to_thread(_search_communities),
        handle_search_code(
            query=query,
            project_paths=[project_path],
            top_k=top_k,
            content_types=["wiki", "knowledge_base", "markdown"],
        ),
    )

    wiki_hits = [
        {
            "type": "wiki",
            "path": r["path"],
            "content": r["content"],
            "score": r["score"],
        }
        for r in wiki_result.get("results", [])
    ]

    all_hits: list[dict[str, Any]] = community_hits + wiki_hits
    all_hits.sort(key=lambda x: x["score"] or 0.0, reverse=True)

    return {
        "query": query,
        "results": all_hits[:top_k],
        "community_matches": len(community_hits),
        "wiki_matches": len(wiki_hits),
        "total": len(all_hits),
    }
