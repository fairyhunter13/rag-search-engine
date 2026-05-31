"""Stdio MCP bridge that auto-starts and forwards to the singleton HTTP daemon."""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP

from opencode_search.daemon import daemon_url, ensure_daemon_running, health_url, stop_daemon

_bridge_client_id = f"bridge-{uuid.uuid4()}"
_heartbeat_task: asyncio.Task[None] | None = None
_workspace_root: Path | None = None


def _get_workspace_root() -> Path:
    """Return the bridge workspace root used for tool scoping.

    The stdio bridge is meant to be run from within a single "opened" workspace
    (Codex/Claude project directory). Without scoping, a model could pass
    arbitrary paths and query/index other projects registered on the machine.
    """
    env_root = os.environ.get("OPENCODE_BRIDGE_WORKSPACE_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()

    global _workspace_root
    if _workspace_root is None:
        _workspace_root = Path.cwd().resolve()
    return _workspace_root


def _allow_outside_workspace() -> bool:
    return os.environ.get("OPENCODE_ALLOW_INDEX_OUTSIDE_CWD", "").strip().lower() in {"1", "true", "yes"}


def _ensure_within_workspace(path: str, *, what: str) -> dict[str, Any] | None:
    """Return an error dict if `path` escapes the workspace, else None."""
    if _allow_outside_workspace():
        return None
    root = _get_workspace_root()
    candidate = Path(path).expanduser().resolve()
    try:
        candidate.relative_to(root)
    except Exception:
        return {
            "status": "error",
            "error": (
                f"{what} is restricted to the currently opened workspace. "
                f"workspace_root={root!s} does not contain requested path={candidate!s}. "
                "Set OPENCODE_ALLOW_INDEX_OUTSIDE_CWD=1 to override."
            ),
        }
    return None


def _post_json(url: str, payload: dict[str, Any]) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5.0):
        return


async def _notify_daemon(path: str, payload: dict[str, Any]) -> None:
    await asyncio.to_thread(_post_json, f"{health_url().removesuffix('/healthz')}{path}", payload)


async def _register_bridge_client() -> None:
    payload = {"client_id": _bridge_client_id, "cwd": os.getcwd()}
    try:
        await _notify_daemon("/admin/client/open", payload)
    except Exception:
        await asyncio.to_thread(stop_daemon)
        await asyncio.to_thread(ensure_daemon_running)
        await _notify_daemon("/admin/client/open", payload)


async def _heartbeat_loop() -> None:
    while True:
        try:
            await asyncio.sleep(15.0)
            await _notify_daemon("/admin/client/heartbeat", {"client_id": _bridge_client_id})
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(5.0)


@asynccontextmanager
async def _bridge_lifespan(_server: FastMCP) -> AsyncIterator[None]:
    global _heartbeat_task
    await asyncio.to_thread(ensure_daemon_running)
    await _register_bridge_client()
    _heartbeat_task = asyncio.create_task(_heartbeat_loop())
    try:
        yield
    finally:
        if _heartbeat_task is not None:
            _heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await _heartbeat_task
            _heartbeat_task = None
        with suppress(Exception):
            await _notify_daemon("/admin/client/close", {"client_id": _bridge_client_id})


bridge = FastMCP(
    name="opencode-search-bridge",
    instructions="Bridge to the singleton opencode-search MCP daemon.",
    lifespan=_bridge_lifespan,
)


async def _forward_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    await asyncio.to_thread(ensure_daemon_running)
    try:
        async with streamable_http_client(daemon_url(), terminate_on_close=False) as streams:
            read_stream, write_stream, _ = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
    except urllib.error.URLError as exc:
        return {"status": "error", "error": str(exc)}

    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured

    content = getattr(result, "content", None) or []
    if len(content) == 1 and getattr(content[0], "type", None) == "text":
        text = getattr(content[0], "text", "")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"result": text}
        return parsed if isinstance(parsed, dict) else {"result": parsed}

    return {"status": "error", "error": "Unexpected bridge response format"}


def _resolve_path_like(value: str) -> str:
    """Resolve a user-supplied path relative to the bridge cwd."""
    if not value:
        return str(Path.cwd().resolve())
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return str(candidate.resolve())


def _nearest_indexed_project(cwd: str, indexed_projects: list[str]) -> str | None:
    """Return the nearest indexed project root that contains cwd."""
    try:
        candidate = Path(cwd).expanduser().resolve()
    except Exception:
        return None

    best: Path | None = None
    for p in indexed_projects:
        try:
            root = Path(p).expanduser().resolve()
            candidate.relative_to(root)
        except Exception:
            continue
        if best is None or len(root.parts) > len(best.parts):
            best = root
    return str(best) if best is not None else None


async def _default_scoped_project_paths() -> list[str] | None:
    """Return [nearest_project_root] for this bridge cwd, or None if unknown."""
    listed = await _forward_tool("overview", {"what": "projects"})
    projects = listed.get("projects", []) if isinstance(listed, dict) else []
    indexed = [p.get("path", "") for p in projects if isinstance(p, dict)]
    indexed = [p for p in indexed if isinstance(p, str) and p]
    cwd = str(Path.cwd().resolve())
    nearest = _nearest_indexed_project(cwd, indexed)
    return [nearest] if nearest else None


# ---------------------------------------------------------------------------
# 7-tool intent API — mirrors opencode_search.mcp exactly, forwards via HTTP
# ---------------------------------------------------------------------------


@bridge.tool()
async def search(
    query: str,
    scope: str = "code",
    project_paths: list[str] | None = None,
    top_k: int = 10,
    include_federation: bool = True,
) -> dict[str, Any]:
    """Find SPECIFIC code, files, or functions matching a natural-language query.

    scope: "code" (default) | "docs" | "all" | "similar"
    project_paths: leave None to auto-scope to the nearest indexed project in cwd.
    For 'how does X work?' questions use `ask` instead.
    """
    if project_paths is None:
        scoped = await _default_scoped_project_paths()
    else:
        scoped = [_resolve_path_like(p) for p in project_paths]
        for p in scoped:
            err = _ensure_within_workspace(p, what="search")
            if err is not None:
                return err
    return await _forward_tool("search", {
        "query": query, "scope": scope, "project_paths": scoped,
        "top_k": top_k, "include_federation": include_federation,
    })


@bridge.tool()
async def ask(
    query: str,
    project_path: str,
    scope: str = "all",
    top_k: int = 10,
    include_federation: bool = True,
) -> dict[str, Any]:
    """Answer 'how does X work?', architecture, or business-process questions.

    scope: "all" (default) | "architecture" | "wiki"
    For finding specific code use `search` instead.
    """
    resolved = _resolve_path_like(project_path)
    err = _ensure_within_workspace(resolved, what="ask")
    if err is not None:
        return err
    return await _forward_tool("ask", {
        "query": query, "project_path": resolved, "scope": scope,
        "top_k": top_k, "include_federation": include_federation,
    })


@bridge.tool()
async def graph(
    symbol: str,
    project_path: str,
    relation: str = "definition",
    to_symbol: str | None = None,
    depth: int = 5,
) -> dict[str, Any]:
    """Explore the call graph: callers, callees, impact blast-radius, or call path.

    relation: "definition" | "callers" | "callees" | "impact" | "path"
    to_symbol: required when relation="path".
    """
    resolved = _resolve_path_like(project_path)
    err = _ensure_within_workspace(resolved, what="graph")
    if err is not None:
        return err
    return await _forward_tool("graph", {
        "symbol": symbol, "project_path": resolved, "relation": relation,
        "to_symbol": to_symbol, "depth": depth,
    })


@bridge.tool()
async def overview(
    project_path: str | None = None,
    what: str = "structure",
    max_depth: int = 4,
    top_k: int = 100,
) -> dict[str, Any]:
    """Get a structural or status overview of a project or the search engine.

    what: "structure" (default) | "communities" | "status" | "projects" | "metrics"
          | "graph_export" | "patterns" (languages, deps, conventions, frameworks, architecture)
    project_path: not required for what="projects" or what="metrics".
    Do NOT use to search code — use `search` or `ask` for that.
    """
    resolved = _resolve_path_like(project_path) if project_path else None
    if resolved:
        err = _ensure_within_workspace(resolved, what="overview")
        if err is not None:
            return err
    return await _forward_tool("overview", {
        "project_path": resolved, "what": what,
        "max_depth": max_depth, "top_k": top_k,
    })


@bridge.tool()
async def build(
    project_path: str,
    action: str = "pipeline",
    source_path: str | None = None,
    symbol: str | None = None,
    max_communities: int = 200,
    include_federation: bool = True,
    force: bool = False,
    watch: bool = True,
) -> dict[str, Any]:
    """Index, enrich, or generate the knowledge base for a project.

    action: "pipeline" (default, full KB build) | "index" | "enrich" | "wiki" |
            "ingest" | "reindex_wiki" | "describe_symbol" |
            "analyze_patterns" (LLM deep pattern analysis, requires LLM provider)
    Only call index/pipeline when the user explicitly asks to index the project.
    """
    resolved = _resolve_path_like(project_path)
    err = _ensure_within_workspace(resolved, what="build")
    if err is not None:
        return err
    return await _forward_tool("build", {
        "project_path": resolved, "action": action, "source_path": source_path,
        "symbol": symbol, "max_communities": max_communities,
        "include_federation": include_federation, "force": force, "watch": watch,
    })


@bridge.tool()
async def federation(
    root_path: str,
    action: str = "list",
    member_path: str | None = None,
    watch: bool = False,
) -> dict[str, Any]:
    """Discover, list, add, remove, or index federation sub-repositories.

    action: "list" (default) | "discover" | "add" | "remove" | "index"
    """
    resolved = _resolve_path_like(root_path)
    err = _ensure_within_workspace(resolved, what="federation")
    if err is not None:
        return err
    return await _forward_tool("federation", {
        "root_path": resolved, "action": action,
        "member_path": member_path, "watch": watch,
    })


@bridge.tool()
async def manage(
    project_path: str,
    action: str = "wiki_lint",
) -> dict[str, Any]:
    """Project lifecycle management: stop watching or lint the wiki.

    action: "wiki_lint" (default) | "stop_watching"
    """
    resolved = _resolve_path_like(project_path)
    err = _ensure_within_workspace(resolved, what="manage")
    if err is not None:
        return err
    return await _forward_tool("manage", {"project_path": resolved, "action": action})


def run_stdio_bridge() -> None:
    bridge.run(transport="stdio")
