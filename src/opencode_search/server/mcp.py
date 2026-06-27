"""FastMCP server: 5 MCP tools — search, ask, graph, overview, index."""
from __future__ import annotations

import json
import time
from typing import NamedTuple

from mcp.server.fastmcp import FastMCP

from opencode_search.daemon.global_prompt import _PROMPT
from opencode_search.daemon.runtime_state import note_activity, note_query
from opencode_search.embed.embedder import get_embedder

mcp = FastMCP("opencode-search", instructions=_PROMPT)


class _ToolInfo(NamedTuple):
    name: str

# Static list of all MCP tools. Update when adding/removing @mcp.tool() handlers.
_MCP_TOOLS: list[_ToolInfo] = [
    _ToolInfo("search"),
    _ToolInfo("ask"),
    _ToolInfo("graph"),
    _ToolInfo("overview"),
    _ToolInfo("index"),
]


def _resolve_roots(requested: list[str]) -> list[str]:
    """Map each requested path to its enclosing registered project root (longest match wins)."""
    from pathlib import Path

    from opencode_search.core.registry import list_projects

    roots = [e.path for e in list_projects() if e.enabled]
    resolved: list[str] = []
    seen: set[str] = set()
    for req in requested:
        if req in roots:
            if req not in seen:
                seen.add(req)
                resolved.append(req)
            continue
        req_p = Path(req)
        best: str | None = None
        for root in roots:
            try:
                req_p.relative_to(root)
                if best is None or len(root) > len(best):
                    best = root
            except ValueError:
                pass
        target = best if best is not None else req
        if target not in seen:
            seen.add(target)
            resolved.append(target)
    return resolved


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

    if project_paths:
        from opencode_search.daemon.federation import expand_federation
        _seen: set[str] = set()
        paths = []
        for _root in _resolve_roots(project_paths):
            for _p in expand_federation(_root):
                if _p not in _seen:
                    _seen.add(_p)
                    paths.append(_p)
    else:
        paths = [p.path for p in list_projects() if p.enabled]
    embedder = get_embedder()
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
    results.sort(key=lambda r: r.get("rerank_score", r.get("score", 0.0)), reverse=True)
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
    """Return assembled context (code chunks + community map) for a codebase question — no LLM synthesis. scope: all|architecture|global|feature|wiki|business. LLM synthesis is the HTTP /api/ask path."""
    note_query(query)
    from opencode_search.query.ask import run_ask
    return run_ask(query, project_path, scope)


@mcp.tool()
async def graph(
    symbol: str,
    project_path: str = "",
    relation: str = "definition",
    to_symbol: str = "",
) -> str:
    """Analyze call graph. relation: definition|callers|callees|impact|impact_narrative|path|semantic_trace."""
    note_activity()
    from opencode_search.query.graph_handler import run_graph
    return run_graph(symbol, project_path, relation, to_symbol)


@mcp.tool()
async def overview(project_path: str = "", what: str = "structure") -> str:
    """Overview of a project. what: structure|communities|status|projects|patterns|metrics|import_cycles|surprising_connections|feature_map|business_rules|process_flows|suggested_questions|service_mesh|validate."""
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
        import shutil

        from opencode_search.core.config import index_dir
        from opencode_search.daemon.federation import expand_federation
        removed = []
        for p in expand_federation(project_path):
            if remove_project(p):
                removed.append(p)
            shutil.rmtree(index_dir(p), ignore_errors=True)
        return json.dumps({"status": "removed", "path": project_path,
                           "members_removed": removed[1:] if len(removed) > 1 else []})
    from pathlib import Path

    from opencode_search.index.discover import is_forbidden_root
    if is_forbidden_root(Path(project_path)):
        return json.dumps({"status": "forbidden", "path": project_path,
                           "note": "registering /tmp or cache directories is not allowed"})
    from opencode_search.core.registry import get_project
    existing = get_project(project_path)
    status = "already_registered" if existing and existing.enabled else "flagged"
    upsert_project(ProjectEntry(path=project_path, enabled=True))
    import threading

    from opencode_search.daemon.sweeps import reconcile_projects
    threading.Thread(target=reconcile_projects, daemon=True).start()
    return json.dumps({"status": status, "path": project_path,
                       "note": "daemon will index, build KB, and watch"})
