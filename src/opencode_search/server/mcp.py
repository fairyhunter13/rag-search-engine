"""FastMCP server: 5 MCP tools — search, ask, graph, overview, index."""
from __future__ import annotations

import json
import time

from mcp.server.fastmcp import FastMCP

from opencode_search.daemon.global_prompt import _PROMPT
from opencode_search.daemon.runtime_state import note_activity, note_query
from opencode_search.embed.embedder import Embedder

_embedder: Embedder | None = None


def _get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = Embedder()
        _embedder.warmup()
    return _embedder


mcp = FastMCP("opencode-search", instructions=_PROMPT)


@mcp.tool()
async def search(
    query: str,
    scope: str = "code",
    project_paths: list[str] | None = None,
) -> str:
    """Search for code semantically. scope: code|docs|all."""
    note_query(query)
    from opencode_search.core.config import project_vector_db
    from opencode_search.core.registry import list_projects
    from opencode_search.index.store import VectorStore
    from opencode_search.query.search import search as _search

    paths = project_paths or [p.path for p in list_projects() if p.enabled]
    embedder = _get_embedder()
    results: list[dict] = []
    t0 = time.monotonic()
    searched: list[str] = []
    for path in paths:
        vdb = project_vector_db(path)
        if not vdb.exists():
            continue
        vs = VectorStore(vdb)
        try:
            results.extend(_search(query, embedder, vs, scope=scope, top_k=10))
            searched.append(path)
        finally:
            vs.close()
    results.sort(key=lambda r: r.get("score", 0), reverse=True)
    return json.dumps({
        "results": results[:10],
        "total": len(results),
        "elapsed_ms": round((time.monotonic() - t0) * 1000),
        "projects_searched": searched,
    })


@mcp.tool()
async def ask(
    query: str,
    project_path: str = "",
    scope: str = "all",
) -> str:
    """Answer a question about the codebase. scope: all|architecture|global|feature|wiki|business."""
    note_query(query)
    from opencode_search.core.config import index_dir, project_graph_db, project_vector_db
    from opencode_search.core.registry import list_projects
    from opencode_search.graph.store import GraphStore
    from opencode_search.index.store import VectorStore
    from opencode_search.kb.answer_cache import get as _cache_get
    from opencode_search.kb.answer_cache import set as _cache_set
    from opencode_search.query.ask import compose_answer
    from opencode_search.query.search import search as _search

    if not project_path:
        projects = [p for p in list_projects() if p.enabled]
        if not projects:
            return "No indexed projects found."
        project_path = projects[0].path

    cache_dir = index_dir(project_path) / "ask_cache"
    cached = _cache_get(cache_dir, f"{scope}:{query}")
    if cached:
        return cached

    vdb, gdb = project_vector_db(project_path), project_graph_db(project_path)
    if not vdb.exists():
        return f"Project not indexed: {project_path}"
    embedder = _get_embedder()
    vs, gs = VectorStore(vdb), GraphStore(gdb)
    try:
        chunks = _search(query, embedder, vs, scope="all", top_k=8)
        answer = compose_answer(query, chunks, gs, scope=scope)
        _cache_set(cache_dir, f"{scope}:{query}", answer, ttl_s=3600)
        return answer
    finally:
        vs.close()
        gs.close()


@mcp.tool()
async def graph(
    symbol: str,
    project_path: str,
    relation: str = "definition",
    to_symbol: str = "",
) -> str:
    """Analyze call graph. relation: definition|callers|callees|impact|impact_narrative|semantic_trace."""
    note_activity()
    from opencode_search.core.config import project_graph_db
    from opencode_search.graph.store import GraphStore
    from opencode_search.query import graph_handler as gh

    gdb = project_graph_db(project_path)
    if not gdb.exists():
        return json.dumps({"error": f"Not indexed: {project_path}"})
    gs = GraphStore(gdb)
    try:
        if relation == "callers":
            return json.dumps({"matches": gh.callers(symbol, gs)})
        if relation == "callees":
            return json.dumps({"matches": gh.callees(symbol, gs)})
        if relation == "impact":
            return json.dumps({"matches": gh.impact(symbol, gs)})
        if relation == "impact_narrative":
            affected = gh.impact(symbol, gs)
            if not affected:
                return json.dumps({"symbol": symbol, "risk": "low", "affected_count": 0,
                                   "summary": f"No callers found for '{symbol}' — low blast radius."})
            names = [r["name"] for r in affected[:20]]
            risk = "high" if len(affected) > 10 else "medium" if len(affected) > 3 else "low"
            return json.dumps({"symbol": symbol, "risk": risk, "affected_count": len(affected),
                               "affected": names,
                               "summary": f"Changing '{symbol}' affects {len(affected)} symbol(s): "
                                          f"{', '.join(names[:5])}{'...' if len(names) > 5 else ''}."})
        if relation == "path":
            if not to_symbol:
                return json.dumps({"error": "relation='path' requires to_symbol"})
            return json.dumps({"path": gh.path_between(symbol, to_symbol, gs)})
        if relation == "semantic_trace":
            if not to_symbol:
                return json.dumps({"error": "relation='semantic_trace' requires to_symbol"})
            path = gh.path_between(symbol, to_symbol, gs)
            if not path:
                return json.dumps({"from": symbol, "to": to_symbol, "path": [],
                                   "summary": f"No call path found from '{symbol}' to '{to_symbol}'."})
            steps = " → ".join(p["name"] for p in path)
            return json.dumps({"from": symbol, "to": to_symbol, "path": path,
                               "summary": f"{symbol} → {to_symbol} via {len(path)} step(s): {steps}"})
        return json.dumps({"matches": gh.definition(symbol, gs)})
    finally:
        gs.close()


@mcp.tool()
async def overview(project_path: str = "", what: str = "structure") -> str:
    """Overview of a project. what: structure|communities|status|projects|patterns."""
    note_activity()
    from opencode_search.server._overview import handle_overview
    return handle_overview(project_path, what)


@mcp.tool()
async def index(project_path: str, enabled: bool = True) -> str:
    """Register (enabled=True) or remove (enabled=False) a project."""
    note_activity()
    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.registry import remove_project, upsert_project

    if not enabled:
        ok = remove_project(project_path)
        import shutil

        from opencode_search.core.config import index_dir
        shutil.rmtree(index_dir(project_path), ignore_errors=True)
        return json.dumps({"status": "removed" if ok else "not_found", "path": project_path})
    from opencode_search.core.registry import get_project
    existing = get_project(project_path)
    status = "already_registered" if existing and existing.enabled else "flagged"
    upsert_project(ProjectEntry(path=project_path, enabled=True))
    return json.dumps({"status": status, "path": project_path,
                       "note": "daemon will auto-index, build KB, and watch"})
