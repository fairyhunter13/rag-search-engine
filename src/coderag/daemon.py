"""The CLI's client for the running daemon, so a search loads no second model.

`coderag search` ran the search in its own process, which built a second CUDA
session beside the daemon's. Measured on one 16 GB card: the daemon holds 7.6 GB
and the CLI adds 3.1 GB, so a third consumer exhausts it and `cublasCreate`
fails. `_GPU_INFER_LOCK` cannot help, because it is a `threading.RLock` and the
two sessions are in different processes.

The `2026-07-28` era carries `roots/list` inside `InputRequiredResult`, so a
stateless client answers the pin in a second POST rather than over a back
channel. The root the caller typed is the root this declares, which is why
delegating needs no relaxation of `scope.require_pin`.

A pipe is what `bridge.py` is. This speaks the protocol, so it is not there.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import config

PROTOCOL = "2026-07-28"
HEADERS = {"content-type": "application/json", "accept": "application/json, text/event-stream"}


class Unreachable(Exception):
    """No daemon answered, so the caller runs the search itself."""


def _payload(text: str) -> dict:
    """Streamable HTTP answers as plain JSON or as SSE, and both are in spec."""
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise Unreachable(f"no JSON-RPC payload in: {text[:200]!r}")


def _post(params: dict) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
    headers = HEADERS | {
        "mcp-protocol-version": PROTOCOL,
        "mcp-method": "tools/call",
        # Checked against the body's own `name`, and a mismatch is refused.
        "mcp-name": params["name"],
    }
    request = urllib.request.Request(config.MCP_URL, data=body.encode(), headers=headers)
    try:
        text = urllib.request.urlopen(request, timeout=config.CLI_TIMEOUT_S).read().decode()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise Unreachable(str(exc)) from exc
    payload = _payload(text)
    if "error" in payload:
        raise Unreachable(str(payload["error"]))
    return payload["result"]


def call(name: str, root: Path | str, **arguments: Any) -> dict:
    """One tool call, answering the daemon's `roots/list` with the named root.

    Two POSTs and no session: the first reply carries an opaque `requestState`,
    and the retry echoes it beside the answers.
    """
    root = str(Path(root).resolve())
    meta = {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL,
        "io.modelcontextprotocol/clientCapabilities": {"roots": {}},
    }
    params = {"_meta": meta, "name": name, "arguments": {"root": root} | arguments}
    result = _post(params)
    requests = result.get("inputRequests")
    if requests:
        answer = {"roots": [{"uri": f"file://{root}"}]}
        params |= {
            "inputResponses": dict.fromkeys(requests, answer),
            "requestState": result["requestState"],
        }
        result = _post(params)
    out = result.get("structuredContent")
    if out is None:
        raise Unreachable(f"no structured content in: {str(result)[:200]!r}")
    return out
