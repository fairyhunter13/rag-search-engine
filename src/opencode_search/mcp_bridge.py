"""Stdio MCP bridge that auto-starts and forwards to the singleton HTTP daemon."""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP

from opencode_search.daemon import daemon_url, ensure_daemon_running, health_url, stop_daemon

_bridge_client_id = f"bridge-{uuid.uuid4()}"
_heartbeat_task: asyncio.Task[None] | None = None


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
            try:
                await _heartbeat_task
            except asyncio.CancelledError:
                pass
            _heartbeat_task = None
        try:
            await _notify_daemon("/admin/client/close", {"client_id": _bridge_client_id})
        except Exception:
            pass


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


@bridge.tool()
async def index_project(
    path: str,
    tier: str = "balanced",
    watch: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    return await _forward_tool(
        "index_project",
        {"path": path, "tier": tier, "watch": watch, "force": force},
    )


@bridge.tool()
async def search_code(
    query: str,
    project_paths: list[str] | None = None,
    top_k: int = 10,
    use_rerank: bool = True,
) -> dict[str, Any]:
    return await _forward_tool(
        "search_code",
        {
            "query": query,
            "project_paths": project_paths,
            "top_k": top_k,
            "use_rerank": use_rerank,
        },
    )


@bridge.tool()
async def project_status(path: str) -> dict[str, Any]:
    return await _forward_tool("project_status", {"path": path})


@bridge.tool()
async def list_indexed_projects() -> dict[str, Any]:
    return await _forward_tool("list_indexed_projects", {})


@bridge.tool()
async def stop_watching(path: str) -> dict[str, Any]:
    return await _forward_tool("stop_watching", {"path": path})


def run_stdio_bridge() -> None:
    bridge.run(transport="stdio")
