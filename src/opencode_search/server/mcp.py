"""FastMCP server: 5 MCP tools — search, ask, graph, overview, index."""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from opencode_search.embed.embedder import Embedder

_embedder: Embedder | None = None


def _get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = Embedder()
        _embedder.warmup()
    return _embedder


mcp = FastMCP(
    "opencode-search",
    instructions=(
        "opencode-search: GPU code intelligence. "
        "Tools: search (find code), ask (explain code), "
        "graph (call relations), overview (project structure), "
        "index (register project)."
    ),
)


@mcp.tool()
async def search(
    query: str,
    scope: str = "code",
    project_paths: list[str] | None = None,
) -> str:
    """Search for code semantically. scope: code|docs|all."""
    from opencode_search.core.config import project_vector_db
    from opencode_search.core.registry import list_projects
    from opencode_search.index.store import VectorStore
    from opencode_search.query.search import search as _search

    paths = project_paths or [p.path for p in list_projects() if p.enabled]
    embedder = _get_embedder()
    results: list[dict] = []
    for path in paths:
        vdb = project_vector_db(path)
        if not vdb.exists():
            continue
        vs = VectorStore(vdb)
        try:
            results.extend(_search(query, embedder, vs, scope=scope, top_k=10))
        finally:
            vs.close()
    results.sort(key=lambda r: r.get("score", 0), reverse=True)
    return json.dumps({"results": results[:10], "count": len(results)})


@mcp.tool()
async def ask(
    query: str,
    project_path: str = "",
    scope: str = "all",
) -> str:
    """Answer a question about the codebase. scope: all|architecture|global|feature|wiki|business."""
    from opencode_search.core.config import project_graph_db, project_vector_db
    from opencode_search.core.registry import list_projects
    from opencode_search.graph.store import GraphStore
    from opencode_search.index.store import VectorStore
    from opencode_search.query.ask import ask as _ask
    from opencode_search.query.search import search as _search

    if not project_path:
        projects = [p for p in list_projects() if p.enabled]
        if not projects:
            return "No indexed projects found."
        project_path = projects[0].path
    vdb, gdb = project_vector_db(project_path), project_graph_db(project_path)
    if not vdb.exists():
        return f"Project not indexed: {project_path}"
    embedder = _get_embedder()
    vs, gs = VectorStore(vdb), GraphStore(gdb)
    try:
        chunks = _search(query, embedder, vs, scope="all", top_k=8)
        return _ask(query, chunks, gs, scope=scope)
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
            return gh.impact_narrative(symbol, gs)
        if relation == "semantic_trace":
            return f"Semantic trace '{symbol}' to '{to_symbol}' not yet implemented."
        return json.dumps({"matches": gh.definition(symbol, gs)})
    finally:
        gs.close()


@mcp.tool()
async def overview(project_path: str = "", what: str = "structure") -> str:
    """Overview of a project. what: structure|communities|status|projects|patterns."""
    from opencode_search.server._overview import handle_overview
    return handle_overview(project_path, what)


@mcp.tool()
async def index(project_path: str, enabled: bool = True) -> str:
    """Register (enabled=True) or remove (enabled=False) a project."""
    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.registry import remove_project, upsert_project

    if not enabled:
        ok = remove_project(project_path)
        return json.dumps({"removed": ok, "path": project_path})
    upsert_project(ProjectEntry(path=project_path, enabled=True))
    return json.dumps({"registered": True, "path": project_path})
