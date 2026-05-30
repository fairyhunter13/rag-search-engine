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
    include_federation: bool = False,
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

    # Build the effective list of project paths (root + federation if requested)
    from opencode_search.config import load_registry
    registry = load_registry()
    effective_paths = [project_path]
    if include_federation:
        from opencode_search.handlers._federation import _expand_with_federation
        effective_paths = _expand_with_federation([project_path], registry)

    def _search_communities_for(path: str) -> list[dict[str, Any]]:
        gs = _open_graph(path)
        if gs is None:
            return []
        try:
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
                        "project_path": path,
                    })
            matches.sort(key=lambda x: x["score"] or 0.0, reverse=True)
            return matches[:top_k]
        finally:
            gs.close()

    def _search_all_communities() -> list[dict[str, Any]]:
        all_matches: list[dict[str, Any]] = []
        for path in effective_paths:
            all_matches.extend(_search_communities_for(path))
        all_matches.sort(key=lambda x: x["score"] or 0.0, reverse=True)
        return all_matches[:top_k]

    from opencode_search.handlers._wiki import handle_wiki_query

    async def _wiki_for_path(path: str) -> list[dict[str, Any]]:
        result = await handle_wiki_query(query=query, project_path=path, top_k=top_k)
        return [
            {
                "type": "wiki",
                "path": r["path"],
                "content": r["content"],
                "score": r["score"],
                "project_path": path,
            }
            for r in result.get("results", [])
        ]

    wiki_tasks = [_wiki_for_path(p) for p in effective_paths]
    community_hits_list, *wiki_hits_per_path = await asyncio.gather(
        asyncio.to_thread(_search_all_communities),
        *wiki_tasks,
    )
    community_hits = community_hits_list
    wiki_hits: list[dict[str, Any]] = []
    for hits in wiki_hits_per_path:
        wiki_hits.extend(hits)
    wiki_hits.sort(key=lambda x: x.get("score") or 0.0, reverse=True)
    wiki_hits = wiki_hits[:top_k]

    all_hits: list[dict[str, Any]] = community_hits + wiki_hits
    all_hits.sort(key=lambda x: x["score"] or 0.0, reverse=True)

    return {
        "query": query,
        "results": all_hits[:top_k],
        "community_matches": len(community_hits),
        "wiki_matches": len(wiki_hits),
        "total": len(all_hits),
    }


async def handle_project_structure(
    project_path: str,
    max_depth: int = 4,
    include_graph_stats: bool = True,
) -> dict[str, Any]:
    """Return a structural overview of the project.

    Produces:
    - Directory tree (up to max_depth levels, skipping common noise dirs)
    - Top-level language breakdown (file counts per language)
    - Graph stats: node/edge/community counts from the code graph
    - Top communities (enriched, largest-first) as architectural anchors
    - Key entry points extracted from the largest communities
    """
    import os
    from collections import Counter

    root = Path(project_path).expanduser().resolve()
    if not root.is_dir():
        return {"error": f"Not a directory: {project_path}"}

    _SKIP_DIRS = {
        ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
        ".pytest_cache", "dist", "build", "target", ".idea", ".vscode",
        "vendor", ".tox", "coverage", ".coverage", "htmlcoverage",
    }

    # Build directory tree
    def _tree(path: Path, depth: int, prefix: str = "") -> list[str]:
        if depth == 0:
            return []
        try:
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except PermissionError:
            return []
        lines: list[str] = []
        visible = [e for e in entries if e.name not in _SKIP_DIRS and not e.name.startswith(".")]
        for i, entry in enumerate(visible[:40]):  # cap dirs shown per level
            connector = "└── " if i == len(visible) - 1 or i == 39 else "├── "
            lines.append(f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}")
            if entry.is_dir() and not entry.is_symlink():
                extension = "    " if i == len(visible) - 1 else "│   "
                lines.extend(_tree(entry, depth - 1, prefix + extension))
        return lines

    tree_lines = [f"{root.name}/"] + _tree(root, max_depth)
    tree_str = "\n".join(tree_lines[:200])  # cap output size

    # Language breakdown from file walk
    lang_counts: Counter = Counter()
    file_count = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for fname in filenames:
                ext = Path(fname).suffix.lower()
                lang_counts[ext] += 1
                file_count += 1
                if file_count > 100_000:
                    break
    except Exception:
        pass

    top_langs = [
        {"extension": ext, "count": cnt}
        for ext, cnt in lang_counts.most_common(15)
        if ext
    ]

    # Graph stats + top communities
    graph_stats: dict[str, Any] = {}
    top_communities: list[dict[str, Any]] = []

    if include_graph_stats:
        gs = _open_graph(project_path)
        if gs is not None:
            try:
                communities = gs.get_communities(limit=10, min_node_count=2, order_by_size=True)
                all_communities = gs.get_communities()
                enriched = sum(1 for c in all_communities if c.title)
                graph_stats = {
                    "total_communities": len(all_communities),
                    "enriched_communities": enriched,
                }
                top_communities = [
                    {
                        "id": c.id,
                        "title": c.title or f"Community {c.id}",
                        "summary": (c.summary or "")[:200],
                        "node_count": c.node_count,
                        "key_entry_points": c.key_entry_points[:3],
                    }
                    for c in communities
                ]
            finally:
                gs.close()

    return {
        "status": "ok",
        "project_path": str(root),
        "file_count": file_count,
        "directory_tree": tree_str,
        "language_breakdown": top_langs,
        "graph_stats": graph_stats,
        "top_communities": top_communities,
    }
