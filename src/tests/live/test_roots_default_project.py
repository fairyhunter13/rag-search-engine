"""Live: an unscoped tool call resolves to the project the MCP client is actually in — advertised
via the MCP *roots* capability — instead of the arbitrary first registry entry (the payment-gateway
bug). No mocks: a real `mcp` SDK client session that declares roots and answers the server's
`roots/list` request over the live streamable-HTTP /mcp transport.
"""
from __future__ import annotations

import json

import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ListRootsResult, Root
from pydantic import FileUrl

from tests.live._sample_workspace import SampleWorkspace

pytestmark = pytest.mark.live

_MCP_URL = "http://127.0.0.1:8765/mcp"


def _roots_cb(paths: list[str]):
    async def cb(_context):
        return ListRootsResult(roots=[Root(uri=FileUrl(f"file://{p}")) for p in paths])
    return cb


async def _graph_no_project_path(advertised_roots: list[str]) -> dict:
    """Call graph{symbol} with NO project_path from a client advertising the given roots."""
    async with (
        streamable_http_client(_MCP_URL) as (read, write, _),
        ClientSession(read, write, list_roots_callback=_roots_cb(advertised_roots)) as s,
    ):
        await s.initialize()
        res = await s.call_tool("graph", {"symbol": "authenticate"})
        return json.loads(res.content[0].text)


async def test_roots_default_resolves_to_client_project(sample_workspace: SampleWorkspace):
    """graph with empty project_path resolves to the project advertised in the client's roots,
    not to projects[0]."""
    from rag_search.core.registry import canonicalize_path

    target = sample_workspace.promo
    data = await _graph_no_project_path([target])
    assert data.get("resolved_project") == canonicalize_path(target), (
        f"expected resolution to advertised root {target}, got {data}"
    )


async def test_unregistered_root_fails_loud(sample_workspace: SampleWorkspace):
    """A client root that isn't a registered project must fail loud with candidates — never a
    silent fall-through to the first registry entry."""
    data = await _graph_no_project_path(["/tmp/rse-not-a-registered-project"])
    assert "project_path required" in data.get("error", ""), data
    assert isinstance(data.get("candidates"), list) and data["candidates"], data
