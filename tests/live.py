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
MODERN_PROTOCOL = "2026-07-28"
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


def daemon_held_mib() -> int:
    """What the daemon under test is already holding.

    The process half of this check excludes the daemon on purpose, and the VRAM
    half has to agree with it or the two contradict: the first live search loads
    12 GB of models, after which every later module reads the suite's own
    working set as somebody else's. Held-by-us is headroom, not contention.
    """
    pids = subprocess.run(
        ["pgrep", "-f", "coderag.cli serve"], capture_output=True, text=True, check=False
    ).stdout.split()
    if not pids:
        return 0
    out = subprocess.run(
        ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        check=False,
    )
    held = 0
    for line in out.stdout.splitlines():
        pid, _, used = line.partition(",")
        if pid.strip() in pids:
            held += int(used.strip())
    return held


def require_clear_gpu() -> None:
    if busy := other_gpu_runs():
        pytest.fail("another GPU run is live; this suite never interleaves:\n" + "\n".join(busy))
    free = free_vram_mib()
    ours = daemon_held_mib()
    if free + ours < MIN_FREE_MIB:
        pytest.fail(
            f"{free} MiB free + {ours} MiB held by the daemon, need {MIN_FREE_MIB}. Stop whatever "
            "else is on the card -- and assert the freed number, never a release call's status code."
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
        self._session: dict[str, str] = {}
        self.client = httpx.Client(timeout=120.0)

    def _post(self, content: str, headers: dict):
        """One retry, and only on a transport error on a reused connection.

        The server closes idle keep-alive connections, and a poll loop that
        happens to hit that window gets `Connection reset by peer` on a request
        the server never saw. Retried once on a fresh connection it either
        succeeds or fails for a reason worth reading -- and a retry on a
        *response* would be the thing that hides a real intermittent fault, so
        this catches only `TransportError`.
        """
        try:
            return self.client.post(self.url, content=content, headers=headers)
        except httpx.TransportError:
            self.client.close()
            self.client = httpx.Client(timeout=120.0)
            return self.client.post(self.url, content=content, headers=headers)

    def call(self, method: str, params: dict | None = None) -> dict:
        if method != "initialize" and not self._session:
            self.handshake()
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        headers = HEADERS | self._session
        response = self._post(json.dumps(body), headers)
        assert response.status_code == 200, f"{method} -> {response.status_code} {response.text}"
        payload = _payload(response.text)
        assert payload.get("id") == self._id, f"reply id {payload.get('id')} != {self._id}"
        assert "error" not in payload, payload["error"]
        if method == "initialize":
            self._adopt(response, payload["result"])
        return payload["result"]

    def handshake(self, version: str = "2025-06-18") -> dict:
        """Called lazily so a single test is a valid run.

        The session used to come from whichever earlier test in the module
        happened to call `initialize` first, so `-k` on any one of them failed
        on a missing session ID -- a suite that only passes whole.
        """
        return self.call(
            "initialize",
            {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "coderag-live-suite", "version": "0.1.0"},
            },
        )

    def _adopt(self, response, result: dict) -> None:
        """A handshake is three messages, not one.

        The server issues `Mcp-Session-Id` on the initialize *response header*
        and rejects every later call without it, and it stays uninitialised
        until the notification arrives. Sending the header back is also what
        makes this a client rather than a curl: a stateful transport that only
        ever sees first messages is never exercised past its first branch.
        """
        session = response.headers.get("mcp-session-id")
        if session:
            self._session["mcp-session-id"] = session
        self._session["mcp-protocol-version"] = result["protocolVersion"]
        ack = self.client.post(
            self.url,
            content=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            headers=HEADERS | self._session,
        )
        assert ack.status_code in (200, 202), f"initialized -> {ack.status_code} {ack.text}"

    def tool(self, name: str, **arguments) -> dict:
        """The tool's own return value, unwrapped from the protocol envelope.

        `structuredContent` is what a caller actually consumes; asserting on the
        text block instead passes while the schema is wrong.
        """
        result = self.call("tools/call", {"name": name, "arguments": arguments})
        assert not result.get("isError"), result
        return result["structuredContent"]

    def modern(self, method: str, params: dict | None = None) -> dict:
        """One call in the `2026-07-28` era: no handshake, no session.

        Three things carry what the handshake used to: the protocol-version
        header, an `mcp-method` header the server checks against the body, and
        the `_meta` envelope. Sending only one of them fails with a message
        about the other, which is why this is a method rather than a flag.
        """
        envelope = {
            "io.modelcontextprotocol/protocolVersion": MODERN_PROTOCOL,
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        return self._modern_post(method, {"_meta": envelope} | (params or {}))

    def _modern_post(self, method: str, params: dict, name: str | None = None) -> dict:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        headers = {"mcp-protocol-version": MODERN_PROTOCOL, "mcp-method": method}
        if name:
            # Checked against the body's own `name`, and a mismatch is refused.
            headers["mcp-name"] = name
        response = self._post(json.dumps(body), HEADERS | headers)
        assert response.status_code == 200, f"{method} -> {response.status_code} {response.text}"
        payload = _payload(response.text)
        assert "error" not in payload, payload["error"]
        return payload["result"]

    def modern_tool(self, name: str, roots: list[str] | None = None, **arguments) -> dict:
        """A `2026-07-28` tool call, answering `roots/list` if the server asks.

        Two POSTs, not a back channel: the server replies `input_required` with
        an opaque `requestState`, and the retry carries the answers plus that
        state verbatim. Declaring `roots` in the envelope is what makes the
        server ask at all, so a client that declares nothing gets one round.
        """
        meta = {
            "io.modelcontextprotocol/protocolVersion": MODERN_PROTOCOL,
            "io.modelcontextprotocol/clientCapabilities": {"roots": {}} if roots else {},
        }
        params = {"_meta": meta, "name": name, "arguments": arguments}
        first = self._modern_post("tools/call", params, name)
        requests = first.get("inputRequests")
        if not requests:
            return first
        answer = {"roots": [{"uri": f"file://{r}"} for r in roots or []]}
        params |= {
            "inputResponses": dict.fromkeys(requests, answer),
            "requestState": first["requestState"],
        }
        return self._modern_post("tools/call", params, name)

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
