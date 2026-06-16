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

    from opencode_search.daemon.federation import expand_federation
    all_paths = expand_federation(project_path)
    if not project_vector_db(project_path).exists():
        return f"Project not indexed: {project_path}"
    embedder = _get_embedder()
    stores = [GraphStore(project_graph_db(p)) for p in all_paths if project_graph_db(p).exists()]
    try:
        chunks: list[dict] = []
        for _p in all_paths:
            _vdb = project_vector_db(_p)
            if not _vdb.exists():
                continue
            _vs = VectorStore(_vdb)
            try:
                chunks.extend(_search(query, embedder, _vs, scope="all", top_k=8))
            finally:
                _vs.close()
        chunks.sort(key=lambda r: r.get("rerank_score", r.get("score", 0.0)), reverse=True)
        answer = compose_answer(query, chunks[:8], stores, scope=scope)
        _cache_set(cache_dir, f"{scope}:{query}", answer, ttl_s=3600)
        return answer
    finally:
        for s in stores:
            s.close()


@mcp.tool()
async def graph(
    symbol: str,
    project_path: str = "",
    relation: str = "definition",
    to_symbol: str = "",
) -> str:
    """Analyze call graph. relation: definition|callers|callees|impact|impact_narrative|path|semantic_trace."""
    note_activity()
    if not project_path:
        from opencode_search.core.registry import list_projects
        projects = [p for p in list_projects() if p.enabled]
        if not projects:
            return json.dumps({"error": "No indexed projects found."})
        project_path = projects[0].path
    from opencode_search.core.config import project_graph_db
    from opencode_search.daemon.federation import expand_federation, federated_map
    from opencode_search.query import graph_handler as gh

    if not any(project_graph_db(p).exists() for p in expand_federation(project_path)):
        return json.dumps({"error": f"Not indexed: {project_path}"})

    _union = {"callers": gh.callers, "callees": gh.callees,
              "impact": gh.impact, "definition": gh.definition}
    if relation in _union:
        _fn = _union[relation]
        matches = [m for _, ms in federated_map(project_path, lambda gs: _fn(symbol, gs)) for m in ms]
        return json.dumps({"matches": matches})
    if relation == "impact_narrative":
        affected = [m for _, ms in federated_map(project_path, lambda gs: gh.impact(symbol, gs)) for m in ms]
        if not affected:
            return json.dumps({"symbol": symbol, "risk": "low", "affected_count": 0,
                               "summary": f"No callers found for '{symbol}' — low blast radius."})
        names = [r["name"] for r in affected[:20]]
        risk = "high" if len(affected) > 10 else "medium" if len(affected) > 3 else "low"
        return json.dumps({"symbol": symbol, "risk": risk, "affected_count": len(affected), "affected": names,
                           "summary": f"Changing '{symbol}' affects {len(affected)} symbol(s): "
                                      f"{', '.join(names[:5])}{'...' if len(names) > 5 else ''}."})
    # path / semantic_trace — per-member; cross-repo paths are not representable
    _note = "call paths are per-member; cross-repo paths are not represented"
    if not to_symbol:
        return json.dumps({"error": f"relation='{relation}' requires to_symbol"})
    for _, path in federated_map(project_path, lambda gs: gh.path_between(symbol, to_symbol, gs)):
        if path:
            steps = " → ".join(p["name"] for p in path)
            return json.dumps({"from": symbol, "to": to_symbol, "path": path, "note": _note,
                               "summary": f"{symbol} → {to_symbol} via {len(path)} step(s): {steps}"})
    return json.dumps({"from": symbol, "to": to_symbol, "path": [], "note": _note,
                       "summary": f"No call path found from '{symbol}' to '{to_symbol}'."
                       if to_symbol else f"relation='{relation}' requires to_symbol"})


@mcp.tool()
async def overview(project_path: str = "", what: str = "structure") -> str:
    """Overview of a project. what: structure|communities|status|projects|patterns|metrics|architecture_domains|hierarchy|import_cycles|surprising_connections|feature_map|business_rules|process_flows|suggested_questions|service_mesh."""
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
