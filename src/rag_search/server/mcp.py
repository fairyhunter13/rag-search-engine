"""FastMCP server: 5 MCP tools — search, ask, graph, overview, index."""
from __future__ import annotations

import asyncio
import json
import time
from typing import NamedTuple

from mcp.server.fastmcp import Context, FastMCP

from rag_search.daemon.global_prompt import _PROMPT
from rag_search.daemon.runtime_state import note_activity, note_query
from rag_search.embed.embedder import get_embedder

mcp = FastMCP("rag-search", instructions=_PROMPT)


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
    """Map each requested path to its registered project (self if it's a registered
    member/root, else its longest enclosing registered root). Canonicalizes first so a
    symlinked federation member scopes to itself rather than fanning out to its parent
    root's whole federation."""
    from rag_search.core.registry import resolve_registered_root

    resolved: list[str] = []
    seen: set[str] = set()
    for req in requested:
        target = resolve_registered_root(req)
        if target not in seen:
            seen.add(target)
            resolved.append(target)
    return resolved


def _search_sync(query: str, scope: str, project_paths: list[str] | None) -> str:
    from rag_search.core.config import project_vector_db
    from rag_search.core.registry import list_projects
    from rag_search.index.store import VectorStore
    from rag_search.query.search import search as _search

    if project_paths:
        from rag_search.daemon.federation import expand_federation
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


async def _roots_paths(ctx: Context) -> list[str]:
    """Best-effort read of the MCP client's advertised workspace roots (its cwd(s)). The daemon
    is a shared global HTTP server with no cwd of its own, so client roots are the only signal for
    "which project is the caller in" when project_path is omitted. Empty if unsupported.

    Must never hang: a client that didn't declare the roots capability would otherwise leave
    `list_roots()` waiting forever for a reply it will never send — so gate on the declared
    capability first, and bound the request with a timeout as a belt-and-suspenders."""
    from mcp.types import ClientCapabilities, RootsCapability
    try:
        sess = ctx.session
        if not sess.check_client_capability(ClientCapabilities(roots=RootsCapability())):
            return []
        res = await asyncio.wait_for(sess.list_roots(), timeout=2.0)
    except Exception:
        return []
    from urllib.parse import unquote, urlparse
    out: list[str] = []
    for r in getattr(res, "roots", None) or []:
        try:
            out.append(unquote(urlparse(str(r.uri)).path))
        except Exception:
            continue
    return out


def _needs_project_error(candidates: list[str]) -> str:
    return json.dumps({
        "error": "project_path required — could not infer a single project from the client's roots. "
                 "Pass project_path explicitly.",
        "candidates": candidates[:12],
    })


async def _default_or_error(ctx: Context, project_path: str) -> tuple[str, str | None]:
    """Resolve an omitted project_path from the client's roots. Returns (path, error_or_None):
    a chosen project when the roots imply exactly one, else a fail-loud error — never a silent
    fall-through to the arbitrary first registry entry."""
    if project_path:
        return project_path, None
    from rag_search.core.registry import infer_default_project
    chosen, cands = infer_default_project(await _roots_paths(ctx))
    if chosen:
        return chosen, None
    return "", _needs_project_error(cands)


@mcp.tool()
async def search(
    query: str,
    scope: str = "code",
    project_paths: list[str] | None = None,
    ctx: Context | None = None,
) -> str:
    """Search for code semantically. scope: code|docs|all."""
    note_query(query)
    if not project_paths:
        # Scope to the project the client is actually in, when inferable from its roots;
        # otherwise keep the existing search-all behavior (broad, never misleading).
        from rag_search.core.registry import infer_default_project
        chosen, _ = infer_default_project(await _roots_paths(ctx))
        if chosen:
            project_paths = [chosen]
    return await asyncio.to_thread(_search_sync, query, scope, project_paths)


@mcp.tool()
async def ask(
    query: str,
    project_path: str = "",
    scope: str = "all",
    ctx: Context | None = None,
) -> str:
    """Return assembled context (code chunks + community map) for a codebase question — no LLM synthesis. scope: all|architecture|global|feature|wiki|business. LLM synthesis is the HTTP /api/ask path."""
    note_query(query)
    project_path, err = await _default_or_error(ctx, project_path)
    if err:
        return err
    from rag_search.query.ask import run_ask
    return await asyncio.to_thread(run_ask, query, project_path, scope)


@mcp.tool()
async def graph(
    symbol: str,
    project_path: str = "",
    relation: str = "definition",
    to_symbol: str = "",
    ctx: Context | None = None,
) -> str:
    """Analyze call graph. relation: definition|callers|callees|impact|impact_narrative|path|semantic_trace."""
    note_activity()
    project_path, err = await _default_or_error(ctx, project_path)
    if err:
        return err
    from rag_search.query.graph_handler import run_graph
    return await asyncio.to_thread(run_graph, symbol, project_path, relation, to_symbol)


@mcp.tool()
async def overview(project_path: str = "", what: str = "structure", ctx: Context | None = None) -> str:
    """Overview of a project. what: structure|communities|status|projects|patterns|metrics|import_cycles|surprising_connections|feature_map|business_rules|process_flows|suggested_questions|service_mesh|validate."""
    note_activity()
    from rag_search.server._overview import _VALID
    # Only a known, project-scoped `what` needs a project. 'projects'/'metrics' are global, and an
    # unknown `what` is a usage error independent of any project — both pass straight through to
    # handle_overview (which validates `what` and returns the valid-set) rather than failing loud
    # on project resolution first.
    if what in _VALID and what not in ("projects", "metrics"):
        project_path, err = await _default_or_error(ctx, project_path)
        if err:
            return err
    from rag_search.server._overview import handle_overview
    return await asyncio.to_thread(handle_overview, project_path, what)


@mcp.tool()
async def index(project_path: str, enabled: bool = True) -> str:
    """Register (enabled=True) or remove (enabled=False) a project."""
    note_activity()
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import canonicalize_path, remove_project, upsert_project

    # Canonicalize only (never enclosing-root-resolve): a write must key the exact
    # target, matching what CLI `init` does, never mis-registering a child under a parent.
    project_path = canonicalize_path(project_path)

    if not enabled:
        import shutil

        from rag_search.core.config import index_dir
        from rag_search.daemon.federation import expand_federation
        removed = []
        for p in expand_federation(project_path):
            if remove_project(p):
                removed.append(p)
            shutil.rmtree(index_dir(p), ignore_errors=True)
        return json.dumps({"status": "removed", "path": project_path,
                           "members_removed": removed[1:] if len(removed) > 1 else []})
    from pathlib import Path

    from rag_search.index.discover import is_forbidden_root
    if is_forbidden_root(Path(project_path)):
        return json.dumps({"status": "forbidden", "path": project_path,
                           "note": "registering /tmp or cache directories is not allowed"})
    from rag_search.core.registry import get_project
    existing = get_project(project_path)
    status = "already_registered" if existing and existing.enabled else "flagged"
    upsert_project(ProjectEntry(path=project_path, enabled=True))
    import threading

    from rag_search.daemon.sweeps import reconcile_projects
    threading.Thread(target=reconcile_projects, daemon=True).start()
    return json.dumps({"status": status, "path": project_path,
                       "note": "daemon will index, build KB, and watch"})
