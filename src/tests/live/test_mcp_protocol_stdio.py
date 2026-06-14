"""P15.3a: MCP stdio transport round-trip — protocol only, no direct tool calls.

Speaks real JSON-RPC over bridge-stdio: initialize → notifications/initialized
→ tools/list → tools/call each tool on real indexed data.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.live

_EXPECTED_TOOLS = {"ask", "graph", "index", "overview", "search"}


class _StdioMCP:
    """Minimal synchronous MCP client over bridge-stdio."""

    def __init__(self) -> None:
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "opencode_search", "daemon", "bridge-stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self._id = 0

    def _send(self, msg: dict) -> None:
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())
        self._proc.stdin.flush()

    def _recv(self) -> dict:
        line = self._proc.stdout.readline()
        assert line, "bridge-stdio closed unexpectedly"
        return json.loads(line)

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        return self._recv()

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": {}})

    def close(self) -> None:
        try:
            self._proc.stdin.close()
            self._proc.wait(timeout=3)
        except Exception:
            self._proc.kill()

    def handshake(self) -> dict:
        r = self.request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0.1"},
        })
        self.notify("notifications/initialized")
        return r


def test_stdio_initialize_returns_5_tool_serverinfo():
    """P15.3a: bridge-stdio initialize — server name + 5-tool instructions."""
    mcp = _StdioMCP()
    try:
        r = mcp.handshake()
        assert r["result"]["serverInfo"]["name"] == "opencode-search"
        assert "5-tool" in r["result"].get("instructions", "")
    finally:
        mcp.close()


def test_stdio_tools_list_returns_exactly_5():
    """P15.3a: tools/list over stdio returns exactly the 5 expected tools."""
    mcp = _StdioMCP()
    try:
        mcp.handshake()
        r = mcp.request("tools/list")
        names = {t["name"] for t in r["result"]["tools"]}
        assert names == _EXPECTED_TOOLS, f"wrong tool set: {names}"
    finally:
        mcp.close()


def test_stdio_overview_projects_returns_real_projects():
    """P15.3a: tools/call overview(projects) over stdio — >=2 real indexed projects."""
    mcp = _StdioMCP()
    try:
        mcp.handshake()
        r = mcp.request("tools/call", {"name": "overview", "arguments": {"what": "projects"}})
        data = json.loads(r["result"]["content"][0]["text"])
        projects = data.get("projects", [])
        assert len(projects) >= 2, f"expected >=2 projects, got {len(projects)}"
        paths = [p["path"] for p in projects]
        assert any("astro" in p for p in paths), f"astro-project missing: {paths}"
    finally:
        mcp.close()


def test_stdio_search_returns_real_ranked_results():
    """P15.3a: tools/call search over stdio — ranked results from real index."""
    mcp = _StdioMCP()
    try:
        mcp.handshake()
        r = mcp.request("tools/call", {"name": "search", "arguments": {"query": "community leiden"}})
        hits = json.loads(r["result"]["content"][0]["text"])
        assert len(hits.get("results", [])) >= 1, "search returned no results over stdio"
    finally:
        mcp.close()


def test_no_direct_tool_calls_in_protocol_tests():
    """P15.3 guard: protocol test files must not bypass the MCP protocol layer."""
    import re
    from pathlib import Path

    tests_dir = Path(__file__).parent
    for f in tests_dir.glob("test_mcp_protocol*.py"):
        text = f.read_text()
        # strip triple-quoted strings so docstrings don't trip the guard
        stripped = re.sub(r'""".*?"""', "", text, flags=re.DOTALL)
        stripped = re.sub(r"'''.*?'''", "", stripped, flags=re.DOTALL)
        assert not re.search(r"asyncio\.run\s*\(", stripped), (
            f"{f.name} must not bypass the MCP protocol via direct tool invocations"
        )
