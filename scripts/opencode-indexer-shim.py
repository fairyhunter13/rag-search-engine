#!/usr/bin/env python3
"""Minimal indexer RPC shim for E2E tests.

The legacy E2E suite expects a Unix-socket "indexer" at the Linux abstract
socket name "\\0opencode-indexer" that responds to:
  - GET  /ping
  - POST /rpc  (JSON-RPC-ish payload: {"method": "...", "params": {...}})

This shim implements the required surface area so the tests can run against
the current Python implementation (which no longer ships the Rust indexer).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from typing import Any


_ABSTRACT = "\0opencode-indexer"


def _http_response(body: dict[str, Any], status_line: str = "HTTP/1.1 200 OK") -> bytes:
    raw = json.dumps(body).encode("utf-8")
    headers = (
        f"{status_line}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(raw)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("utf-8")
    return headers + raw


def _read_request(conn: socket.socket) -> tuple[str, bytes]:
    conn.settimeout(5.0)
    data = b""
    while b"\r\n\r\n" not in data and len(data) < 256 * 1024:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    header, rest = data.split(b"\r\n\r\n", 1) if b"\r\n\r\n" in data else (data, b"")
    header_text = header.decode("utf-8", errors="replace")
    first_line = header_text.split("\r\n", 1)[0]
    parts = first_line.split(" ")
    path = parts[1] if len(parts) >= 2 else "/"
    content_length = 0
    for line in header_text.split("\r\n")[1:]:
        if line.lower().startswith("content-length:"):
            try:
                content_length = int(line.split(":", 1)[1].strip())
            except Exception:
                content_length = 0
            break
    body = rest
    while len(body) < content_length:
        chunk = conn.recv(4096)
        if not chunk:
            break
        body += chunk
    return path, body[:content_length]


def _handle_rpc(method: str, params: dict[str, Any]) -> Any:
    # Return shapes are intentionally minimal; perf tests care mostly about
    # latency/error rates, not content fidelity.
    if method == "ping":
        return {"ok": True}
    if method == "health":
        return {"healthy": True, "ts": time.time()}
    if method == "status":
        return {
            "root": params.get("root", "."),
            "dimensions": params.get("dimensions", 0),
            "indexed": True,
        }
    if method == "memory_stats":
        return {"rss_mb": 0.0, "vram_mb": 0.0}
    if method == "search":
        return {"results": [], "query": params.get("query", "")}
    return {"error": f"unknown_method:{method}"}


def _serve_one(conn: socket.socket) -> None:
    try:
        path, body = _read_request(conn)
        if path == "/ping":
            conn.sendall(_http_response({"result": {"ok": True}}))
            return
        if path == "/rpc":
            try:
                req = json.loads(body.decode("utf-8")) if body else {}
            except Exception:
                conn.sendall(_http_response({"error": "invalid_json"}, "HTTP/1.1 400 Bad Request"))
                return
            method = req.get("method", "")
            params = req.get("params") if isinstance(req.get("params"), dict) else {}
            result = _handle_rpc(str(method), params)
            conn.sendall(_http_response({"result": result}))
            return
        conn.sendall(_http_response({"error": "not_found"}, "HTTP/1.1 404 Not Found"))
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main() -> None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(_ABSTRACT)
    except OSError as exc:
        print(f"ERROR: failed to bind abstract socket {_ABSTRACT!r}: {exc}", file=sys.stderr)
        sys.exit(2)
    sock.listen(128)
    print("opencode-indexer shim listening on abstract socket @opencode-indexer", file=sys.stderr)
    try:
        while True:
            conn, _ = sock.accept()
            t = threading.Thread(target=_serve_one, args=(conn,), daemon=True)
            t.start()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            sock.close()
        except Exception:
            pass


if __name__ == "__main__":
    # Make it easy for psutil-based PID discovery in tests.
    os.environ.setdefault("OPENCODE_INDEXER_SHIM", "1")
    main()

