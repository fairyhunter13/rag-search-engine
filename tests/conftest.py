"""Pytest fixtures for E2E resource efficiency and performance tests.

Service discovery:
  - Embedder: EMBEDDER_URL env var (default: http://127.0.0.1:9998)
  - Indexer:  port read from ~/.opencode/indexer.port

All HTTP-dependent tests are auto-skipped when the service is unavailable.

To run live HTTP tests:
    # Start the embedder with idle-shutdown disabled:
    OPENCODE_EMBED_IDLE_SHUTDOWN=0 OPENCODE_DISABLE_TENSORRT=1 \\
    OPENCODE_SEARCH_EMBEDDER_MAX_RSS_MB=8192 HF_HUB_OFFLINE=1 \\
    OPENCODE_EMBED_LOW_MEMORY=1 python -m opencode_embedder.server &

    # Then run tests:
    EMBEDDER_URL=http://127.0.0.1:9998 python -m pytest e2e_resource_efficiency.py -v
"""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

import psutil
import pytest

EMBEDDER_URL = os.environ.get("EMBEDDER_URL", "http://127.0.0.1:9998")
INDEXER_PORT_FILE = Path.home() / ".opencode" / "indexer.port"


def _strict_no_skip_enabled() -> bool:
    return os.environ.get("OPENCODE_FAIL_ON_SKIP", "").strip().lower() in {"1", "true", "yes", "on"}


def _read_indexer_port() -> int | None:
    try:
        return int(INDEXER_PORT_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


_ABSTRACT_SOCKET_NAME = "\0opencode-indexer"


def _check_abstract_socket() -> bool:
    """Return True if the Linux abstract socket \\0opencode-indexer responds to GET /ping."""
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


def _rpc_call_file_socket(sock_path: str, method: str, params: dict | None = None, timeout: float = 10.0) -> Any:
    """JSON-RPC 2.0 over file-based Unix socket (HTTP framing)."""
    payload = json.dumps(
        {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1}
    )
    headers = (
        f"POST /rpc HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        f"Content-Type: application/json\r\n"
        f"Connection: close\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"\r\n"
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


def _rpc_call_abstract_socket(socket_name: str, method: str, params: dict | None = None, timeout: float = 10.0) -> Any:
    """JSON-RPC 2.0 over Linux abstract Unix socket (HTTP framing)."""
    payload = json.dumps(
        {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1}
    )
    headers = (
        f"POST /rpc HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        f"Content-Type: application/json\r\n"
        f"Connection: close\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"\r\n"
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
                    header_part = response[:header_end].decode("utf-8", errors="replace")
                    for line in header_part.split("\r\n"):
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


def _check_url(url: str, timeout: float = 3.0) -> bool:
    """Return True if the URL responds with HTTP 200."""
    import urllib.request
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


# Standalone helpers (used by e2e_performance.py imports)
def _embedder_alive(url: str = "") -> bool:
    """Return True if the embedder health endpoint responds with 200."""
    check_url = url or EMBEDDER_URL
    return _check_url(f"{check_url}/health", timeout=3)


def _find_embedder_pid() -> int | None:
    """Scan /proc for the embedder process."""
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        cmdline = " ".join(proc.info.get("cmdline") or [])
        if "opencode_embedder" in cmdline or "opencode-embedder" in cmdline:
            return proc.info["pid"]
    return None


def _find_indexer_pid() -> int | None:
    """Scan /proc for the indexer process."""
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        name = proc.info.get("name") or ""
        cmdline = " ".join(proc.info.get("cmdline") or [])
        if "opencode-indexer" in name or "opencode-indexer" in cmdline:
            return proc.info["pid"]
    return None


@pytest.fixture(scope="session")
def embedder_url() -> str:
    # Re-read at fixture time so EMBEDDER_URL env is resolved after arg processing
    return os.environ.get("EMBEDDER_URL", "http://127.0.0.1:9998")


@pytest.fixture(scope="session")
def indexer_url() -> str | None:
    port = _read_indexer_port()
    if port:
        return f"http://127.0.0.1:{port}"
    if _check_abstract_socket():
        return "abstract://@opencode-indexer"
    return None


@pytest.fixture(scope="session")
def embedder_alive(embedder_url):
    """Skip if embedder not running."""
    if not _check_url(f"{embedder_url}/health"):
        if _strict_no_skip_enabled():
            pytest.fail(f"Embedder not reachable at {embedder_url}/health")
        pytest.skip(f"Embedder not reachable at {embedder_url}/health")
    return True


@pytest.fixture(scope="session")
def indexer_alive(indexer_url):
    """Skip if indexer not running."""
    if indexer_url is None:
        if _strict_no_skip_enabled():
            pytest.fail("~/.opencode/indexer.port not found and abstract socket unreachable; indexer not running")
        pytest.skip("~/.opencode/indexer.port not found and abstract socket unreachable; indexer not running")
    if indexer_url.startswith("abstract://"):
        return True
    if not _check_url(f"{indexer_url}/ping"):
        if _strict_no_skip_enabled():
            pytest.fail(f"Indexer not reachable at {indexer_url}/ping")
        pytest.skip(f"Indexer not reachable at {indexer_url}/ping")
    return True




@pytest.fixture(scope="session")
def embedder_pid(embedder_alive) -> int:
    """PID of the running embedder process."""
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        cmdline = " ".join(proc.info.get("cmdline") or [])
        if "opencode_embedder" in cmdline or "opencode-embedder" in cmdline:
            return proc.info["pid"]
    # Fall back to python processes serving on the embedder port
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        cmdline = " ".join(proc.info.get("cmdline") or [])
        if "server.py" in cmdline and "embedder" in cmdline:
            return proc.info["pid"]
    if _strict_no_skip_enabled():
        pytest.fail("Could not identify embedder PID")
    pytest.skip("Could not identify embedder PID")


@pytest.fixture(scope="session")
def indexer_pid(indexer_alive) -> int:
    """PID of the running indexer process."""
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        name = proc.info.get("name") or ""
        cmdline = " ".join(proc.info.get("cmdline") or [])
        if "opencode-indexer" in name or "opencode-indexer" in cmdline:
            return proc.info["pid"]
    if _strict_no_skip_enabled():
        pytest.fail("Could not identify indexer PID")
    pytest.skip("Could not identify indexer PID")


def pytest_sessionfinish(session, exitstatus):
    """Fail strict validation runs if pytest reported any skipped tests."""
    if not _strict_no_skip_enabled():
        return
    skipped = session.config.pluginmanager.get_plugin("terminalreporter").stats.get("skipped", [])
    if skipped and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED

