"""FastMCP server: 4 MCP tools — search, graph, overview, index.

`ask` was the fifth. It returned assembled prose — chunk bodies and community summaries
concatenated and hard-truncated at 3000 chars each — which is the shape an MCP tool should not
return: the client is an agent that can call again, so it wants compact references it chooses to
expand, not context someone else assembled and cut off mid-chunk. It was also the most expensive
tool here (a GPU embed, the full federation fan-out, and *two* cross-encoder passes) and its code
half duplicated `search` while returning a worse shape. The architecture half it uniquely reached
now lives on `overview(what="communities", query=...)`, and `query/ask.py` survives as the context
builder for the two callers that genuinely cannot loop: the CLI and the dashboard's chat.

A static `_MCP_TOOLS` list stood here, described as "update when adding/removing @mcp.tool()
handlers". Nothing read it. `mcp.list_tools()` is the registry, and a mirror nobody consults is a
second source of truth that can only ever be wrong.
"""
from __future__ import annotations

import asyncio
import json
import time

from mcp.server.fastmcp import Context, FastMCP

from rag_search.daemon.global_prompt import _PROMPT
from rag_search.daemon.runtime_state import note_activity, note_query
from rag_search.embed.embedder import get_embedder

mcp = FastMCP("rag-search", instructions=_PROMPT)


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
    # Resolve to db paths only — `search_federation` opens and closes the stores, a batch at a
    # time. Nothing here holds a connection, which is the point.
    #
    # This used to open every member up front. Two separate defects lived in that shape and both
    # bit on 2026-07-29. The first is volume: one search over the 194-member root opens 157 stores
    # at ~3 fds each under WAL, so a single question held 471 descriptors against systemd's
    # default `LimitNOFILESoft` of 1024, and a concurrent `overview(what="communities")` (a
    # GraphStore per member) put it over. The second is that the opening loop sat *above* the
    # try/finally, so the EMFILE that volume produced orphaned every store already opened — the
    # list was still local, nothing closed it, and the fds were held until the process died. The
    # daemon ended up holding 957 fds with `/healthz` returning 500 because `accept()` itself
    # could no longer get one, and every project's search failed with `unable to open database
    # file` until it was restarted. Moving the loop inside the try fixed the leak but left the
    # 471-descriptor peak; batching inside `search_federation` removes the peak too, and a
    # failure to open now costs one query rather than the daemon.
    searched: list[str] = []
    dbs = []
    for path in paths:
        vdb = project_vector_db(path)
        if not vdb.exists():
            continue
        dbs.append(vdb)
        searched.append(path)
    # One embed and one global rerank for the whole federation. Looping search() per project
    # instead costs an extra GPU embed and an extra rerank batch per member — 194 of each for
    # one question on the largest federation here — and concatenates rankings that were never
    # scored against each other.
    results = search_federation(query, get_embedder(), dbs, scope=scope, top_k=top_k)
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
async def overview(project_path: str = "", what: str = "structure", query: str = "",
                   ctx: Context | None = None) -> str:
    """Overview of a project. what: structure|communities|status|projects|metrics|import_cycles|surprising_connections|validate.

    `query` applies to what="communities" only: it ranks the community map by relevance to your
    question, which is the architecture axis — use it when you need what a project is *shaped*
    like rather than where a symbol is. Use `search` for locations.
    """
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
    return await asyncio.to_thread(handle_overview, project_path, what, query)


def _store_mb(store_dir) -> float:  # type: ignore[no-untyped-def]
    """On-disk size of one index dir, in MB. 0.0 for a dir that is missing or unreadable.

    The preview's whole job is to make the cost legible before it is paid, so a store it cannot
    measure is reported as 0.0 rather than raising — a size lookup must not be what stops a caller
    from removing a project.
    """
    if not store_dir.exists():
        return 0.0
    total = 0
    for f in store_dir.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total / 1_048_576


@mcp.tool()
async def index(project_path: str, enabled: bool = True, confirm_members: bool = False) -> str:
    """Register (enabled=True) or remove (enabled=False) a project.

    Removing a federation root removes every member with it and deletes their stores. That fans out
    silently from one path to N, so it returns a preview instead and does nothing until you call it
    again with confirm_members=True. A single project needs no confirmation.
    """
    note_activity()
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import canonicalize_path, remove_project, upsert_project

    # Canonicalize only (never enclosing-root-resolve): a write must key the exact
    # target, matching what CLI `init` does, never mis-registering a child under a parent.
    project_path = canonicalize_path(project_path)

    if not enabled:
        from rag_search.core.config import index_dir
        from rag_search.core.orphans import quarantine
        from rag_search.daemon.federation import expand_federation
        targets = expand_federation(project_path)
        # The asymmetry that makes this worth a round trip: a membership row the daemon deletes here
        # is rediscovered on the next federation walk, but the embeddings under it are not. Undoing
        # this costs GPU time proportional to the members, and the caller asked about one path.
        if len(targets) > 1 and not confirm_members:
            return json.dumps({
                "status": "confirm_required", "path": project_path,
                "members": targets[1:],
                "stores": [{"path": p, "mb": round(_store_mb(index_dir(p)), 1)} for p in targets],
                "note": (f"removing {project_path} also removes {len(targets) - 1} federation "
                         "member(s) and deletes every store listed; re-embedding them is the cost "
                         "of undoing it. Nothing was deleted. Repeat with confirm_members=True."),
            })
        removed = []
        for p in targets:
            if remove_project(p):
                removed.append(p)
            quarantine(index_dir(p))
        # The confirmation above is honest about the cost of undoing this, and the quarantine is
        # what makes undoing it possible at all for a week. Both, not either: the note tells a
        # caller who is about to be wrong, the trash serves the one who already was.
        return json.dumps({"status": "removed", "path": project_path,
                           "members_removed": removed[1:] if len(removed) > 1 else [],
                           "recoverable_until_days": 7})
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
