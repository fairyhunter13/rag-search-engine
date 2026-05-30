"""Standalone p95 latency checker for the search daemon.

Run from repo root:
    python scripts/p95_check.py <project_path>

Exits 0 on pass, 1 on failure.
Does NOT import lancedb — safe to run while daemon holds lance files open.
Uses os._exit() at the end to skip Python interpreter shutdown cleanup.
FastMCP's module-level bridge object hangs during normal GC after asyncio.run()
closes the event loop (attempts to cancel async tasks with a dead loop).
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, "src")

_GATE_S = 5.0
_QUERIES = ["authentication", "database connection", "handler", "parse", "config"]


async def _search(project_path: str, query: str) -> float:
    from opencode_search.mcp_bridge import _forward_tool

    t0 = time.monotonic()
    await _forward_tool("search_code", {
        "query": query,
        "project_paths": [project_path],
        "use_rerank": False,
    })
    return time.monotonic() - t0


async def main(project_path: str) -> int:
    # Unmeasured warmup: loads ONNX models + warms LanceDB page cache in daemon.
    print("warmup...", flush=True)
    await _search(project_path, "warmup")
    print("warmup done", flush=True)

    latencies = []
    for q in _QUERIES:
        elapsed = await _search(project_path, q)
        latencies.append(elapsed)
        print(f"  {q!r}: {elapsed*1000:.0f}ms", flush=True)

    p95 = sorted(latencies)[int(len(latencies) * 0.95)]
    print(f"p95={p95*1000:.0f}ms gate={_GATE_S*1000:.0f}ms", flush=True)
    if p95 > _GATE_S:
        print(f"FAIL: p95 {p95*1000:.0f}ms exceeds {_GATE_S*1000:.0f}ms gate", flush=True)
        return 1
    print("PASS", flush=True)
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/p95_check.py <project_path>")
        os._exit(2)
    rc = asyncio.run(main(sys.argv[1]))
    # os._exit skips Python interpreter shutdown (GC, atexit, __del__).
    # FastMCP's module-level bridge object has async cleanup that deadlocks
    # when the asyncio event loop is already closed by asyncio.run().
    os._exit(rc)
