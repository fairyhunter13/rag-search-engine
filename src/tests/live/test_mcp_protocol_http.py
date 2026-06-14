"""P15.3b: MCP streamable-HTTP /mcp transport round-trip — no asyncio.run(tool()) calls.

Sends real JSON-RPC requests to the live daemon's /mcp endpoint (SSE response
format; Accept: application/json, text/event-stream). Also asserts parity with
the stdio transport (same 5-tool set from both).
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest
import requests

pytestmark = pytest.mark.live

_BASE = "http://127.0.0.1:8765"
_MCP_URL = f"{_BASE}/mcp"
_HDR = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
_EXPECTED_TOOLS = {"ask", "graph", "index", "overview", "search"}


def _sse_json(r: requests.Response) -> dict:
    for line in r.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise AssertionError(f"no data: line in SSE response: {r.text[:300]}")


def _http_session() -> tuple[dict, str]:
    r = requests.post(_MCP_URL, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0.1"}},
    }, headers=_HDR, timeout=10)
    assert r.status_code == 200, f"initialize failed {r.status_code}"
    sid = r.headers.get("mcp-session-id", "")
    h = {**_HDR, "mcp-session-id": sid} if sid else _HDR
    return h, sid


def test_http_mcp_initialize_returns_5_tool_serverinfo():
    """P15.3b: /mcp initialize — server name + 5-tool instructions (SSE)."""
    r = requests.post(_MCP_URL, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0.1"}},
    }, headers=_HDR, timeout=10)
    assert r.status_code == 200
    data = _sse_json(r)
    assert data["result"]["serverInfo"]["name"] == "opencode-search"
    assert "5-tool" in data["result"].get("instructions", "")


def test_http_mcp_tools_list_returns_exactly_5():
    """P15.3b: /mcp tools/list returns exactly the 5 expected tools (SSE)."""
    h, _ = _http_session()
    r = requests.post(_MCP_URL, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                      headers=h, timeout=10)
    assert r.status_code == 200
    names = {t["name"] for t in _sse_json(r)["result"]["tools"]}
    assert names == _EXPECTED_TOOLS, f"wrong tool set over HTTP: {names}"


def test_http_mcp_overview_projects_returns_real_projects():
    """P15.3b: /mcp tools/call overview(projects) — >=2 real indexed projects (SSE)."""
    h, _ = _http_session()
    r = requests.post(_MCP_URL, json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "overview", "arguments": {"what": "projects"}},
    }, headers=h, timeout=10)
    assert r.status_code == 200
    data = json.loads(_sse_json(r)["result"]["content"][0]["text"])
    assert len(data.get("projects", [])) >= 2


def test_http_mcp_search_returns_real_ranked_results():
    """P15.3b: /mcp tools/call search — ranked real results (SSE)."""
    h, _ = _http_session()
    r = requests.post(_MCP_URL, json={
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "search", "arguments": {"query": "authentication handler"}},
    }, headers=h, timeout=15)
    assert r.status_code == 200
    hits = json.loads(_sse_json(r)["result"]["content"][0]["text"])
    assert len(hits.get("results", [])) >= 1, "search returned no results over HTTP"


def test_stdio_and_http_return_same_tool_set():
    """P15.3: parity — stdio and /mcp streamable-HTTP expose the exact same 5 tools."""
    # stdio
    proc = subprocess.Popen(
        [sys.executable, "-m", "opencode_search", "daemon", "bridge-stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0.1"},
        }}) + "\n"
        proc.stdin.write(msg.encode())
        proc.stdin.flush()
        proc.stdout.readline()  # initialize response
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n").encode())
        proc.stdin.flush()
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n").encode())
        proc.stdin.flush()
        stdio_names = {t["name"] for t in json.loads(proc.stdout.readline())["result"]["tools"]}
    finally:
        proc.stdin.close()
        proc.wait(timeout=3)

    # HTTP
    h, _ = _http_session()
    r = requests.post(_MCP_URL, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                      headers=h, timeout=10)
    http_names = {t["name"] for t in _sse_json(r)["result"]["tools"]}

    assert stdio_names == http_names == _EXPECTED_TOOLS, (
        f"transport mismatch — stdio:{stdio_names} http:{http_names}"
    )
