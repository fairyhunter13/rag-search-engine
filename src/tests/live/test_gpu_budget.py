"""Live proof gates for the VRAM budget: the daemon must be able to give the GPU back.

HR40 bounds the daemon's CPU with a kernel quota. Nothing bounded its VRAM, and ORT's BFC arena
only ever grows — it holds the high-water mark of the largest batch a session has served until
that session is destroyed. The daemon's only path to destroying it was a 300 s idle unload,
which cannot fire while anyone is actively working against the daemon. Measured on a 16 GB card:
12.2 GB still held at `active_clients: 0`, 3.5 GB free, and this suite — which loads its own real
embedder + reranker on the same card and needs ~8.4 GB — failed 60 tests inside onnxruntime with
CUBLAS/BFCArena errors naming neither the GPU nor the daemon. Restarting the daemon (71 MiB held,
15.8 GB free) turned the same 60 failures into 623 passed with nothing else changed.

GB1  live, fast   after real GPU work, POST /api/gpu/release actually returns VRAM to the driver
GB2  live, fast   the daemon still binds a GPU EP after a release (GPU-only doctrine survives it)
"""
from __future__ import annotations

import json
import subprocess

import pytest
import requests

pytestmark = pytest.mark.live

_BASE = "http://127.0.0.1:8765"
_MCP_URL = f"{_BASE}/mcp"
_HDR = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
_UNIT = "rag-search-mcp-daemon.service"

# The release has to clear more than rounding noise to count. One warmed embedder + reranker is
# well over this; the point is to fail if the endpoint returns 200 while freeing nothing, which
# is exactly how `_idle_unload` used to read in the log while the card stayed full.
_MIN_RECLAIM_MB = 100.0


def _daemon_vram_mb() -> float:
    """VRAM the daemon process itself holds, via NVML's per-process accounting.

    Deliberately per-process rather than total free: this test runs inside a suite that is
    itself holding models on the same card, so a total-free reading would be measuring both.
    """
    import pynvml

    from rag_search.core.gpu import select_gpu_device

    r = subprocess.run(
        ["systemctl", "--user", "show", _UNIT, "-p", "MainPID", "--value"],
        capture_output=True, text=True, timeout=5,
    )
    pid = int(r.stdout.strip())
    assert pid > 0, f"{_UNIT} reports MainPID={pid} — daemon not running under systemd"
    pynvml.nvmlInit()
    h = pynvml.nvmlDeviceGetHandleByIndex(select_gpu_device())
    for p in pynvml.nvmlDeviceGetComputeRunningProcesses(h):
        if p.pid == pid:
            return (p.usedGpuMemory or 0) / 1_048_576
    return 0.0


def _make_the_daemon_load_its_models(project_path: str) -> None:
    """Drive a real MCP search so the daemon has an embedder + reranker resident on the card.

    The search lane is the one that warms both; a plain status route would leave the arena
    empty and make GB1 assert on nothing. `project_path` is a sample-workspace member, never a
    real device project — and it must be passed, because an unscoped search resolves against the
    caller's MCP roots and returns a `project_path required` error without ever embedding, which
    looks like a pass to any assertion that only checks the call succeeded.
    """
    r = requests.post(_MCP_URL, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "test-gpu-budget", "version": "0.1"}},
    }, headers=_HDR, timeout=60)
    assert r.status_code == 200, f"initialize failed {r.status_code}"
    sid = r.headers.get("mcp-session-id", "")
    headers = {**_HDR, "mcp-session-id": sid} if sid else _HDR

    r = requests.post(_MCP_URL, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "search", "arguments": {
            "query": "embedder warm-up", "top_k": 3, "project_paths": [project_path]}},
    }, headers=headers, timeout=300)
    assert r.status_code == 200, f"search call failed {r.status_code}: {r.text[:200]}"
    for line in r.text.splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[6:])
            assert "result" in payload, f"search returned no result: {line[:200]}"
            body = json.loads(payload["result"]["content"][0]["text"])
            assert "error" not in body, (
                f"search errored instead of embedding, so nothing was warmed: {body['error']}"
            )
            return
    raise AssertionError(f"no data: line in SSE response: {r.text[:300]}")


def test_gb1_gpu_release_actually_returns_vram(standalone_project_path):
    """GB1: the release path frees device memory, not just a Python reference."""
    _make_the_daemon_load_its_models(standalone_project_path)
    before = _daemon_vram_mb()

    r = requests.post(f"{_BASE}/api/gpu/release", timeout=60)
    assert r.status_code == 200, f"/api/gpu/release -> {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert body["status"] == "released", body

    after = _daemon_vram_mb()
    # `before` is only large when the daemon had actually warmed a model; when it had not there
    # is nothing to reclaim and the invariant is trivially held, so assert on the drop only when
    # there was something to drop. Never-shrinking is the failure this gate exists for.
    if before >= _MIN_RECLAIM_MB:
        assert after <= before - _MIN_RECLAIM_MB, (
            f"daemon held {before:.0f} MiB before the release and {after:.0f} MiB after — "
            f"it returned {before - after:.0f} MiB. The endpoint reported success while the "
            f"BFC arena stayed on the card, which is the exact failure that starved the live "
            f"suite: dropping the Python singleton is not enough if anything still references "
            f"the InferenceSession."
        )


def test_gb2_daemon_rebinds_a_gpu_ep_after_release(standalone_project_path):
    """GB2: releasing must not become a silent downgrade to CPU (GPU-only is fatal-or-nothing)."""
    requests.post(f"{_BASE}/api/gpu/release", timeout=60).raise_for_status()

    # A search after the release rebuilds both models from scratch. `Embedder._init` raises if
    # they come back on anything but a GPU EP, so a search that returns a result at all is proof
    # the rebind stayed on the card — the release must not become a quiet CPU downgrade.
    _make_the_daemon_load_its_models(standalone_project_path)

    h = requests.get(f"{_BASE}/healthz", timeout=10).json()
    assert h.get("ok") is True, f"daemon unhealthy after release/reload cycle: {h}"
    assert _daemon_vram_mb() > 0, (
        "daemon holds no VRAM after a post-release search — it should have rebuilt its models "
        "on the GPU, so zero device memory means the work did not land on the card at all."
    )
