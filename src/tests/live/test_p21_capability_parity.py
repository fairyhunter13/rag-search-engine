"""P21: Capability-parity tests — docstring↔handler parity + round-trips + check_system smoke.

These guard against:
  A1: overview docstring advertising fewer what= values than _overview._VALID
  A2: graph docstring omitting the 'path' relation
  B3: overview round-trips for the 10 previously-undocumented what= handlers
  B4: check_system.py exits 0 against the live daemon
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.live

_BASE = "http://127.0.0.1:8765"
_MCP_URL = f"{_BASE}/mcp"
_HDR = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

_GRAPH_SUPPORTED_RELATIONS = {
    "definition", "callers", "callees", "impact",
    "impact_narrative", "path", "semantic_trace",
}
_REPO = Path(__file__).resolve().parent.parent.parent.parent  # repo root


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
    }, headers=_HDR, timeout=60)  # generous — pool socket inherits this for reused connections
    assert r.status_code == 200
    sid = r.headers.get("mcp-session-id", "")
    h = {**_HDR, "mcp-session-id": sid} if sid else _HDR
    return h, sid


def _mcp_overview(h: dict, what: str, project_path: str = "", timeout: int = 60) -> dict:
    args: dict = {"what": what}
    if project_path:
        args["project_path"] = project_path
    r = requests.post(_MCP_URL, json={
        "jsonrpc": "2.0", "id": 99, "method": "tools/call",
        "params": {"name": "overview", "arguments": args},
    }, headers=h, timeout=timeout)
    assert r.status_code == 200, f"HTTP {r.status_code} for overview({what})"
    return json.loads(_sse_json(r)["result"]["content"][0]["text"])


# ---------------------------------------------------------------------------
# A1 guard: overview docstring must list every what= in _overview._VALID
# ---------------------------------------------------------------------------

def test_overview_docstring_matches_valid_set():
    """A1 guard: overview.__doc__ must advertise every what= supported by _overview._VALID."""
    from rag_search.server._overview import _VALID
    from rag_search.server.mcp import overview

    doc = overview.__doc__ or ""
    # Extract tokens after "what:"
    m = re.search(r"what:\s*([\w|]+)", doc)
    assert m, f"overview docstring has no 'what:' section: {doc!r}"
    doc_tokens = set(m.group(1).split("|"))
    assert doc_tokens == _VALID, (
        f"overview docstring tokens {sorted(doc_tokens)} != _VALID {sorted(_VALID)}"
    )


# ---------------------------------------------------------------------------
# A2 guard: graph docstring must include every supported relation
# ---------------------------------------------------------------------------

def test_graph_docstring_matches_supported_relations():
    """A2 guard: graph.__doc__ must list all supported relation= values."""
    from rag_search.server.mcp import graph

    doc = graph.__doc__ or ""
    m = re.search(r"relation:\s*([\w|]+)", doc)
    assert m, f"graph docstring has no 'relation:' section: {doc!r}"
    doc_tokens = set(m.group(1).split("|"))
    assert doc_tokens == _GRAPH_SUPPORTED_RELATIONS, (
        f"graph docstring relations {sorted(doc_tokens)} != supported {sorted(_GRAPH_SUPPORTED_RELATIONS)}"
    )


# ---------------------------------------------------------------------------
# B3: overview round-trips for all 15 what= values via real /mcp transport
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("what", sorted([
    "structure", "communities", "status", "projects", "patterns",
    "metrics", "import_cycles",
    "surprising_connections", "feature_map", "business_rules",
    "process_flows", "suggested_questions", "service_mesh",
]))
def test_overview_what_round_trip(what):
    """B3: overview({what}) over real /mcp transport returns non-error JSON.

    Project-scoped `what`s now require an explicit project_path (an omitted one with no client
    roots fails loud by design), so scope them to the first enabled project; 'projects'/'metrics'
    are global and need none."""
    h, _ = _http_session()
    proj = ""
    if what not in ("projects", "metrics"):
        enabled = [p["path"] for p in _mcp_overview(h, "projects").get("projects", []) if p.get("enabled")]
        assert enabled, "no enabled project to scope overview round-trip"
        proj = enabled[0]
    data = _mcp_overview(h, what, project_path=proj)
    assert "error" not in data or data.get("error") is None, (
        f"overview({what}) returned error: {data}"
    )


# ---------------------------------------------------------------------------
# B4: check_system.py exits 0 against the live daemon
# ---------------------------------------------------------------------------

def test_check_system_exits_zero():
    """B4: scripts/check_system.py exits 0 (all required checks pass) with live daemon."""
    check_script = _REPO / "scripts" / "check_system.py"
    assert check_script.exists(), f"check_system.py not found at {check_script}"
    result = subprocess.run(
        [sys.executable, str(check_script)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"check_system.py exited {result.returncode}\n"
        f"stdout:\n{result.stdout[-2000:]}\n"
        f"stderr:\n{result.stderr[-500:]}"
    )
    assert "All required checks passed." in result.stdout
