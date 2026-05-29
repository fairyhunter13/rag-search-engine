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
    """Return [current_project_root] for this bridge cwd, or None if unknown."""
    listed = await _forward_tool("list_indexed_projects", {})
    projects = listed.get("projects", []) if isinstance(listed, dict) else []
    indexed = [p.get("path", "") for p in projects if isinstance(p, dict)]
    indexed = [p for p in indexed if isinstance(p, str) and p]
    cwd = str(Path.cwd().resolve())
    nearest = _nearest_indexed_project(cwd, indexed)
    return [nearest] if nearest else None


@bridge.tool()
async def index_project(
    path: str,
    watch: bool = False,
    force: bool = False,
    follow_symlinks: bool = True,
) -> dict[str, Any]:
    resolved = _resolve_path_like(path)
    err = _ensure_within_workspace(resolved, what="index_project")
    if err is not None:
        return err
    return await _forward_tool(
        "index_project",
        {"path": resolved, "watch": watch, "force": force, "follow_symlinks": follow_symlinks},
    )


@bridge.tool()
async def search_code(
    query: str,
    project_paths: list[str] | None = None,
    top_k: int = 10,
    use_rerank: bool = True,
) -> dict[str, Any]:
    scoped_paths: list[str] | None = None
    if project_paths is None:
        scoped_paths = await _default_scoped_project_paths()
        if not scoped_paths:
            return {
                "status": "error",
                "error": (
                    "No indexed project contains the current working directory. "
                    "Run index_project on the current project root or pass explicit project_paths."
                ),
            }
    else:
        scoped_paths = [_resolve_path_like(p) for p in project_paths]
        for p in scoped_paths:
            err = _ensure_within_workspace(p, what="search_code")
            if err is not None:
                return err
    return await _forward_tool(
        "search_code",
        {
            "query": query,
            "project_paths": scoped_paths,
            "top_k": top_k,
            "use_rerank": use_rerank,
        },
    )


@bridge.tool()
async def get_symbol(name: str, project_path: str) -> dict[str, Any]:
    """Find a function, class, or method by name or qualified name."""
    resolved = _resolve_path_like(project_path)
    return await _forward_tool("get_symbol", {"name": name, "project_path": resolved})


@bridge.tool()
async def get_callers(
    symbol: str,
    project_path: str,
    depth: int = 5,
) -> dict[str, Any]:
    """Who calls this function? BFS upstream traversal up to `depth` hops."""
    resolved = _resolve_path_like(project_path)
    return await _forward_tool(
        "get_callers", {"symbol": symbol, "project_path": resolved, "depth": depth},
    )


@bridge.tool()
async def get_callees(
    symbol: str,
    project_path: str,
    depth: int = 5,
) -> dict[str, Any]:
    """What does this function call? BFS downstream traversal up to `depth` hops."""
    resolved = _resolve_path_like(project_path)
    return await _forward_tool(
        "get_callees", {"symbol": symbol, "project_path": resolved, "depth": depth},
    )


@bridge.tool()
async def trace_path(
    from_symbol: str,
    to_symbol: str,
    project_path: str,
) -> dict[str, Any]:
    """Find the shortest call path from one symbol to another."""
    resolved = _resolve_path_like(project_path)
    return await _forward_tool(
        "trace_path",
        {"from_symbol": from_symbol, "to_symbol": to_symbol, "project_path": resolved},
    )


@bridge.tool()
async def detect_impact(symbol: str, project_path: str) -> dict[str, Any]:
    """Blast radius: everything that transitively calls this symbol."""
    resolved = _resolve_path_like(project_path)
    return await _forward_tool("detect_impact", {"symbol": symbol, "project_path": resolved})


@bridge.tool()
async def get_communities(project_path: str) -> dict[str, Any]:
    """Return Leiden community clusters for the project."""
    resolved = _resolve_path_like(project_path)
    return await _forward_tool("get_communities", {"project_path": resolved})


@bridge.tool()
async def global_search(query: str, project_path: str, top_k: int = 10) -> dict[str, Any]:
    """Search across architectural knowledge: community summaries and wiki pages."""
    resolved = _resolve_path_like(project_path)
    return await _forward_tool("global_search", {"query": query, "project_path": resolved, "top_k": top_k})


@bridge.tool()
async def enrich_project(project_path: str, scope: str = "communities") -> dict[str, Any]:
    """Trigger LLM enrichment via Ollama. scope: 'symbols'|'communities'|'wiki'|'all'."""
    resolved = _resolve_path_like(project_path)
    return await _forward_tool("enrich_project", {"project_path": resolved, "scope": scope})


@bridge.tool()
async def get_symbol_intent(name: str, project_path: str) -> dict[str, Any]:
    """Get LLM-generated plain-English description of what a function or class does."""
    resolved = _resolve_path_like(project_path)
    return await _forward_tool("get_symbol_intent", {"name": name, "project_path": resolved})


@bridge.tool()
async def wiki_generate(project_path: str) -> dict[str, Any]:
    """Auto-generate wiki pages for all modules and communities in the project."""
    resolved = _resolve_path_like(project_path)
    return await _forward_tool("wiki_generate", {"project_path": resolved})


@bridge.tool()
async def wiki_ingest(source_path: str, project_path: str) -> dict[str, Any]:
    """Ingest a raw document (markdown notes, PDF, design doc) into the project wiki."""
    resolved_project = _resolve_path_like(project_path)
    return await _forward_tool("wiki_ingest", {"source_path": source_path, "project_path": resolved_project})


@bridge.tool()
async def wiki_query(query: str, project_path: str, top_k: int = 5) -> dict[str, Any]:
    """Search wiki pages and community summaries."""
    resolved = _resolve_path_like(project_path)
    return await _forward_tool("wiki_query", {"query": query, "project_path": resolved, "top_k": top_k})


@bridge.tool()
async def wiki_lint(project_path: str) -> dict[str, Any]:
    """Health-check the wiki: find orphaned pages, stale content, missing cross-references."""
    resolved = _resolve_path_like(project_path)
    return await _forward_tool("wiki_lint", {"project_path": resolved})


@bridge.tool()
async def search_docs(
    query: str,
    project_paths: list[str] | None = None,
    top_k: int = 10,
) -> dict[str, Any]:
    """Search only documentation files (markdown, rst, txt, wiki pages)."""
    resolved = [_resolve_path_like(p) for p in project_paths] if project_paths else None
    return await _forward_tool("search_docs", {"query": query, "project_paths": resolved, "top_k": top_k})


def run_stdio_bridge() -> None:
    bridge.run(transport="stdio")
