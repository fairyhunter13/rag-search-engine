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


_PREVIEW_CHARS = 200


def _project(r: dict, verbosity: str) -> dict:
    """A compact result carries a location and a preview; a full one carries the chunk body.

    Compact is the default because the caller is an agent with a context window. A body it did
    not ask for costs tokens on every single call, while path + line range is all it needs to
    Read the exact region for the few hits it actually cares about.
    """
    if verbosity == "full":
        return r
    return {
        "path": r.get("path"),
        "start_line": r.get("start_line"),
        "end_line": r.get("end_line"),
        "language": r.get("language"),
        "score": round(float(r.get("rerank_score", r.get("score", 0.0))), 4),
        "preview": " ".join((r.get("content") or "").split())[:_PREVIEW_CHARS],
    }


def _search_sync(
    query: str, scope: str, project_paths: list[str] | None, top_k: int, verbosity: str
) -> str:
    from rag_search.core.config import project_vector_db
    from rag_search.daemon.federation import expand_federation
    from rag_search.index.store import VectorStore
    from rag_search.query.search import search_federation

    # `project_paths` is guaranteed non-empty: `search` runs the resolution ladder first and
    # returns an error rather than calling in unscoped. There is deliberately no else-branch —
    # the one this replaced opened all 160 enabled stores whenever roots inference came up
    # empty, which is every client that doesn't advertise the roots capability.
    _seen: set[str] = set()
    paths = []
    for _root in _resolve_roots(project_paths):
        for _p in expand_federation(_root):
            if _p not in _seen:
                _seen.add(_p)
                paths.append(_p)
    t0 = time.monotonic()
    searched: list[str] = []
    stores: list[VectorStore] = []
    for path in paths:
        vdb = project_vector_db(path)
        if not vdb.exists():
            continue
        # migrate=False: a query must never pay a store's one-time FTS backfill. Measured at
        # 11.3 s for a single 99 k-chunk member, and this loop opens one store per federation
        # member. Reconcile does open stores writable and so does migrate them, but it is not the
        # convergence path it looks like: measured across the hours after this shipped it moved
        # 137 owed stores to 136, because its walk is spent on real indexing work and every live
        # test run pauses it. The backfill is run as an explicit one-time fleet migration.
        stores.append(VectorStore(vdb, migrate=False))
        searched.append(path)
    # One embed and one global rerank for the whole federation. Looping search() per project
    # instead costs an extra GPU embed and an extra rerank batch per member — 194 of each for
    # one question on the largest federation here — and concatenates rankings that were never
    # scored against each other.
    try:
        results = search_federation(query, get_embedder(), stores, scope=scope, top_k=top_k)
    finally:
        for vs in stores:
            vs.close()
    return json.dumps({
        "results": [_project(r, verbosity) for r in results],
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
                 "Pass project_path explicitly, or add ?project=<path> to the MCP server URL.",
        "candidates": candidates[:12],
    })


def _scope_from_request(ctx: Context | None) -> tuple[str, str | None]:
    """The project this *connection* was configured for, from `?project=` on the MCP URL.

    Returns (project, error). An empty project with no error means the URL carried no scope at
    all, which is the pre-existing case and falls through to roots inference.

    This rung exists because the daemon is one global HTTP server with no cwd of its own, and
    MCP roots are an *optional* client capability — a client that doesn't advertise them leaves
    every tool guessing, and `search` used to guess "all 160 projects". The URL is the one
    channel every client carries regardless of capability, which makes scoping a property of
    the configuration rather than of what the client volunteers.

    An unregistered `?project=` is a loud error, not an empty result set: a typo in the URL
    would otherwise open zero stores and return a confident "no matches" for every query.
    """
    from urllib.parse import unquote
    try:
        req = ctx.request_context.request if ctx is not None else None
        raw = req.query_params.get("project", "") if req is not None else ""
    except Exception:
        return "", None
    if not raw:
        return "", None
    from rag_search.core.config import project_vector_db
    from rag_search.core.registry import list_projects, resolve_registered_root
    target = resolve_registered_root(unquote(raw))
    enabled = [p.path for p in list_projects() if p.enabled]
    # Enabled *or* holding a store: a federation member is a legitimate scope even when it is
    # not a registry entry in its own right — 157 of redacted-name-10's 194 members are enabled, and the
    # rest are still searched when the root is the scope.
    if target not in enabled and not project_vector_db(target).exists():
        return "", json.dumps({
            "error": f"MCP URL is scoped to ?project={raw!r}, which is neither a registered "
                     "enabled project nor an indexed federation member. Fix the server URL, "
                     "or register it with index().",
            "resolved_to": target,
            "candidates": enabled[:12],
        })
    return target, None


async def _default_or_error(ctx: Context, project_path: str) -> tuple[str, str | None]:
    """Resolve an omitted project_path. Returns (path, error_or_None).

    The ladder, most specific first: the explicit argument, then the `?project=` this
    connection was configured with, then the client's roots when they imply exactly one
    project, then a fail-loud error. Never a silent fall-through — not to the arbitrary first
    registry entry, and not to "everything".

    Whatever comes out is a *root*, and callers hand it to `expand_federation`, so scoping to a
    federation root still serves the root and all of its members.
    """
    if project_path:
        return project_path, None
    scoped, err = _scope_from_request(ctx)
    if err:
        return "", err
    if scoped:
        return scoped, None
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
    top_k: int = 8,
    verbosity: str = "compact",
    ctx: Context | None = None,
) -> str:
    """Search for code semantically. scope: code|docs|all.

    Returns ranked locations — path, line range, score, and a short preview — to Read.
    Pass verbosity="full" when you need the whole chunk body inline instead.
    """
    note_query(query)
    if not project_paths:
        # Same ladder as every other tool, and the same fail-loud ending. This used to fall
        # through to "search all enabled projects", which measured 164.78 s against 7.01 s for
        # the same query scoped to one — and answered a question about *this* repo with
        # redacted-name-13-lp JavaScript. Breadth was not free and it was not "never misleading".
        chosen, err = await _default_or_error(ctx, "")
        if err:
            return err
        project_paths = [chosen]
    return await asyncio.to_thread(_search_sync, query, scope, project_paths, top_k, verbosity)


@mcp.tool()
async def ask(
    query: str,
    project_path: str = "",
    scope: str = "all",
    ctx: Context | None = None,
) -> str:
    """Return assembled context (code chunks + community map) for a codebase question — no LLM synthesis. scope: all|architecture — `all` leads with the code axis, `architecture` leads with the community map. LLM synthesis is the HTTP /api/ask path."""
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
    """Analyze call graph. relation: definition|callers|callees|impact|impact_narrative|path."""
    note_activity()
    project_path, err = await _default_or_error(ctx, project_path)
    if err:
        return err
    from rag_search.query.graph_handler import run_graph
    return await asyncio.to_thread(run_graph, symbol, project_path, relation, to_symbol)


@mcp.tool()
async def overview(project_path: str = "", what: str = "structure", ctx: Context | None = None) -> str:
    """Overview of a project. what: structure|communities|status|projects|metrics|import_cycles|surprising_connections|suggested_questions|validate."""
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
