"""Shared infrastructure helpers for e2e tests (embedder/indexer service discovery)."""
from __future__ import annotations

import json
import os
import socket
import urllib.request
from pathlib import Path

import psutil

EMBEDDER_URL = os.environ.get("EMBEDDER_URL", "http://127.0.0.1:9998")
INDEXER_PORT_FILE = Path.home() / ".opencode" / "indexer.port"
_ABSTRACT_SOCKET_NAME = "\0opencode-indexer"


def strict_no_skip() -> bool:
    return os.environ.get("OPENCODE_FAIL_ON_SKIP", "").strip().lower() in {"1", "true", "yes", "on"}


def read_indexer_port() -> int | None:
    try:
        return int(INDEXER_PORT_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def check_abstract_socket() -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(3.0)
            s.connect(_ABSTRACT_SOCKET_NAME)
            s.sendall(b"GET /ping HTTP/1.0\r\nHost: localhost\r\n\r\n")
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        return data.startswith(b"HTTP/") and b" 200 " in data.split(b"\r\n")[0]
    except Exception:
        return False


def rpc_call_file_socket(sock_path: str, method: str, params: dict | None = None, timeout: float = 10.0):
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1})
    headers = (
        f"POST /rpc HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
        f"Connection: close\r\nContent-Length: {len(payload)}\r\n\r\n"
    )
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(sock_path)
        sock.sendall((headers + payload).encode())
        response = b""
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
        except socket.timeout:
            pass
    finally:
        sock.close()
    if b"\r\n\r\n" not in response:
        raise RuntimeError(f"No HTTP response body. Raw: {response[:200]}")
    body = response.split(b"\r\n\r\n", 1)[1]
    parsed = json.loads(body)
    return parsed.get("result", parsed)


def rpc_call_abstract_socket(socket_name: str, method: str, params: dict | None = None, timeout: float = 10.0):
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1})
    headers = (
        f"POST /rpc HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
        f"Connection: close\r\nContent-Length: {len(payload)}\r\n\r\n"
    )
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        abstract_addr = "\x00" + socket_name.lstrip("@")
        sock.connect(abstract_addr)
        sock.sendall((headers + payload).encode())
        response = b""
        content_length: int | None = None
        header_end: int = -1
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
                if header_end == -1 and b"\r\n\r\n" in response:
                    header_end = response.index(b"\r\n\r\n") + 4
                    for line in response[:header_end].decode("utf-8", errors="replace").split("\r\n"):
                        if line.lower().startswith("content-length:"):
                            content_length = int(line.split(":", 1)[1].strip())
                            break
                if content_length is not None and header_end != -1:
                    if len(response) - header_end >= content_length:
                        break
        except socket.timeout:
            pass
    finally:
        sock.close()
    if b"\r\n\r\n" not in response:
        raise RuntimeError(f"No HTTP response body. Raw: {response[:200]}")
    body = response.split(b"\r\n\r\n", 1)[1]
    parsed = json.loads(body)
    return parsed.get("result", parsed)


def check_url(url: str, timeout: float = 3.0) -> bool:
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def embedder_alive(url: str = "") -> bool:
    return check_url(f"{url or EMBEDDER_URL}/health", timeout=3)


def find_embedder_pid() -> int | None:
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        cmdline = " ".join(proc.info.get("cmdline") or [])
        if "opencode_embedder" in cmdline or "opencode-embedder" in cmdline:
            return proc.info["pid"]
    return None


def find_indexer_pid() -> int | None:
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        name = proc.info.get("name") or ""
        cmdline = " ".join(proc.info.get("cmdline") or [])
        if "opencode-indexer" in name or "opencode-indexer" in cmdline:
            return proc.info["pid"]
    return None
