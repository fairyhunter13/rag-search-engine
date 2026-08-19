"""Preconditions and a raw JSON-RPC client for the live suite.

Two preconditions, and the run is invalid without both: nothing else may be
holding the card, and there must be enough of it free. They are checked here
rather than in each test because a suite that skips half way through a busy GPU
reports a mixture of red, green and skipped that nobody can read.

The client here is deliberately raw -- `httpx` and a dict, not the `mcp`
package's own client. This layer exists to prove the wire format, and a test
that speaks the protocol through the same library the server does will agree
with the server about a shape they are both wrong about.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

import httpx
import pytest

from coderag import config

MIN_FREE_MIB = 10 * 1024
OTHER_GPU_WORK = ("tests/eval.py", "coderag.cli serve", "coderag serve")
HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


def free_vram_mib() -> int:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        pytest.fail(f"nvidia-smi failed; this suite needs a GPU: {out.stderr.strip()}")
    return int(out.stdout.strip().splitlines()[0])


def other_gpu_runs(exclude_daemon: bool = True) -> list[str]:
    """Anything else on this machine that would share the card.

    The daemon itself is excluded because it is the thing under test; every
    other holder is a reason to refuse. Editing parallelises across sessions and
    live testing does not, and two runs sharing a 16 GB card do not fail
    cleanly -- they produce numbers, and the numbers are wrong.
    """
    found = []
    for pattern in OTHER_GPU_WORK:
        if exclude_daemon and "serve" in pattern:
            continue
        out = subprocess.run(["pgrep", "-af", pattern], capture_output=True, text=True, check=False)
        found += [line for line in out.stdout.splitlines() if str(os.getpid()) not in line]
    return found


def require_clear_gpu() -> None:
    if busy := other_gpu_runs():
        pytest.fail("another GPU run is live; this suite never interleaves:\n" + "\n".join(busy))
    free = free_vram_mib()
    if free < MIN_FREE_MIB:
        pytest.fail(
            f"{free} MiB free, need {MIN_FREE_MIB}. Stop the daemon rather than trusting the "
            "idle timer -- and assert the freed number, never a release call's status code."
        )


def require_daemon(url: str = "", timeout: float = 5.0) -> str:
    """The daemon has to be up already. Starting one here would hide the unit."""
    base = (url or config.MCP_URL).rsplit("/mcp", 1)[0]
    try:
        health = httpx.get(f"{base}/healthz", timeout=timeout)
    except httpx.HTTPError as exc:
        pytest.fail(f"no daemon at {base}: {exc}. `systemctl --user start coderag`.")
    assert health.status_code == 200, health.text
    return url or config.MCP_URL


class Rpc:
    """One JSON-RPC caller. `id` increments so a stale reply cannot pass as a fresh one."""

    def __init__(self, url: str):
        self.url = url
        self._id = 0
        self.client = httpx.Client(timeout=120.0)

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        response = self.client.post(self.url, content=json.dumps(body), headers=HEADERS)
        assert response.status_code == 200, f"{method} -> {response.status_code} {response.text}"
        payload = _payload(response.text)
        assert payload.get("id") == self._id, f"reply id {payload.get('id')} != {self._id}"
        assert "error" not in payload, payload["error"]
        return payload["result"]

    def tool(self, name: str, **arguments) -> dict:
        """The tool's own return value, unwrapped from the protocol envelope.

        `structuredContent` is what a caller actually consumes; asserting on the
        text block instead passes while the schema is wrong.
        """
        result = self.call("tools/call", {"name": name, "arguments": arguments})
        assert not result.get("isError"), result
        return result["structuredContent"]

    def close(self) -> None:
        self.client.close()


def _payload(text: str) -> dict:
    """Streamable HTTP answers as plain JSON or as SSE, and both are in spec."""
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(f"no JSON-RPC payload in: {text[:200]!r}")


def until(predicate, *, timeout: float, interval: float = 0.5, what: str = "condition"):
    """Poll rather than sleep once: the watcher's debounce and the index queue
    have no completion signal a caller can subscribe to, and a fixed sleep is
    either a flake or a slow suite."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}; last value: {last!r}")
