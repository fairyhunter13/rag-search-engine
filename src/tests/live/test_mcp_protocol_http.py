"""P15.3b: MCP streamable-HTTP /mcp transport round-trip — no asyncio.run(tool()) calls.

Sends real JSON-RPC requests to the live daemon's /mcp endpoint (SSE response
format; Accept: application/json, text/event-stream). Also asserts parity with
the stdio transport (same 5-tool set from both). Data calls target sample_workspace.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest
import requests

from tests.live._sample_workspace import SampleWorkspace

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def sample_proj_path(sample_workspace: SampleWorkspace) -> str:
    """promo-svc path from sample_workspace — used to scope HTTP protocol search calls."""
    return sample_workspace.promo

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
    assert data["result"]["serverInfo"]["name"] == "rag-search"
    instructions = data["result"].get("instructions", "")
    assert "5-tool" in instructions
    for tool in ("search", "ask", "graph", "overview", "index"):
        assert tool in instructions, f"MCP initialize instructions missing tool '{tool}'"


def test_http_mcp_tools_list_returns_exactly_5():
    """P15.3b: /mcp tools/list returns exactly the 5 expected tools (SSE)."""
    h, _ = _http_session()
    r = requests.post(_MCP_URL, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                      headers=h, timeout=10)
    assert r.status_code == 200
    names = {t["name"] for t in _sse_json(r)["result"]["tools"]}
    assert names == _EXPECTED_TOOLS, f"wrong tool set over HTTP: {names}"


def test_http_mcp_overview_projects_returns_indexed_sample_projects(sample_proj_path):
    """P15.3b: /mcp tools/call overview(projects) — >=2 sample projects indexed (SSE)."""
    h, _ = _http_session()
    r = requests.post(_MCP_URL, json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "overview", "arguments": {"what": "projects"}},
    }, headers=h, timeout=10)
    assert r.status_code == 200
    data = json.loads(_sse_json(r)["result"]["content"][0]["text"])
    assert len(data.get("projects", [])) >= 2


def test_http_mcp_search_returns_sample_ranked_results(sample_proj_path):
    """P15.3b: /mcp tools/call search — ranked results from sample promo-svc (SSE)."""
    h, _ = _http_session()
    r = requests.post(_MCP_URL, json={
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "search", "arguments": {
            "query": "promo apply discount",
            "project_paths": [sample_proj_path],
        }},
    }, headers=h, timeout=15)
    assert r.status_code == 200
    hits = json.loads(_sse_json(r)["result"]["content"][0]["text"])
    assert len(hits.get("results", [])) >= 1, (
        f"search returned no results from sample promo-svc over HTTP; hits={hits}"
    )


def test_stdio_and_http_return_same_tool_set():
    """P15.3: parity — stdio and /mcp streamable-HTTP expose the exact same 5 tools."""
    # stdio
    proc = subprocess.Popen(
        [sys.executable, "-m", "rag_search", "daemon", "bridge-stdio"],
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


def test_http_overview_unknown_what_returns_error_and_valid():
    """G4 e2e: /mcp overview{what:'bogus'} returns error + valid list over real MCP transport."""
    h, _ = _http_session()
    r = requests.post(_MCP_URL, json={
        "jsonrpc": "2.0", "id": 10, "method": "tools/call",
        "params": {"name": "overview", "arguments": {"what": "bogus_unknown_what"}},
    }, headers=h, timeout=10)
    assert r.status_code == 200
    data = json.loads(_sse_json(r)["result"]["content"][0]["text"])
    assert "error" in data, f"G4: expected error key, got: {data}"
    assert "valid" in data, f"G4: expected valid key, got: {data}"
    assert "structure" in data["valid"], f"G4: 'structure' missing from valid: {data['valid']}"


def test_http_graph_no_project_path_no_roots_fails_loud():
    """Empty project_path from a client advertising no roots must FAIL LOUD with candidates —
    never silently answer about the arbitrary first registry project (payment-gateway bug)."""
    h, _ = _http_session()
    r = requests.post(_MCP_URL, json={
        "jsonrpc": "2.0", "id": 11, "method": "tools/call",
        "params": {"name": "graph", "arguments": {"symbol": "authenticate"}},
    }, headers=h, timeout=15)
    assert r.status_code == 200
    data = json.loads(_sse_json(r)["result"]["content"][0]["text"])
    assert "project_path required" in data.get("error", ""), f"expected fail-loud, got: {data}"
    assert isinstance(data.get("candidates"), list) and data["candidates"], data


def test_http_overview_structure_has_files_with_symbols(sample_proj_path):
    """G3 e2e: /mcp overview{what:'structure'} exposes files_with_symbols (not file_count)."""
    h, _ = _http_session()
    r = requests.post(_MCP_URL, json={
        "jsonrpc": "2.0", "id": 12, "method": "tools/call",
        "params": {"name": "overview", "arguments": {"what": "structure", "project_path": sample_proj_path}},
    }, headers=h, timeout=10)
    assert r.status_code == 200
    data = json.loads(_sse_json(r)["result"]["content"][0]["text"])
    assert "files_with_symbols" in data, f"G3: expected files_with_symbols, got keys: {list(data)}"
    assert "file_count" not in data, f"G3: old file_count key must be gone, got: {list(data)}"


def test_http_overview_status_keeps_file_count(sample_proj_path):
    """G3 e2e: /mcp overview{what:'status'} keeps file_count (registry value, canonical)."""
    h, _ = _http_session()
    r = requests.post(_MCP_URL, json={
        "jsonrpc": "2.0", "id": 13, "method": "tools/call",
        "params": {"name": "overview", "arguments": {"what": "status", "project_path": sample_proj_path}},
    }, headers=h, timeout=10)
    assert r.status_code == 200
    data = json.loads(_sse_json(r)["result"]["content"][0]["text"])
    assert "file_count" in data, f"G3: status must keep file_count (registry), got: {list(data)}"
