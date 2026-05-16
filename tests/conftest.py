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
import time
from pathlib import Path
from typing import Any

import psutil
import pytest

EMBEDDER_URL = os.environ.get("EMBEDDER_URL", "http://127.0.0.1:9998")
INDEXER_PORT_FILE = Path.home() / ".opencode" / "indexer.port"


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
        pytest.skip(f"Embedder not reachable at {embedder_url}/health")
    return True


@pytest.fixture(scope="session")
def indexer_alive(indexer_url):
    """Skip if indexer not running."""
    if indexer_url is None:
        pytest.skip("~/.opencode/indexer.port not found and abstract socket unreachable; indexer not running")
    if indexer_url.startswith("abstract://"):
        return True
    if not _check_url(f"{indexer_url}/ping"):
        pytest.skip(f"Indexer not reachable at {indexer_url}/ping")
    return True


@pytest.fixture(scope="session")
def indexer_rpc(indexer_url, indexer_alive):
    """Return a callable rpc(method, params=None) that talks to the indexer.
    
    NOTE: The abstract socket path is exercised in dev (Linux) but has no
    automated test coverage on CI since the indexer daemon is not running
    there. File-based socket path is currently untested — no conftest
    fixture creates a file-socket indexer.
    """
    if indexer_url.startswith("abstract://"):
        def _abstract_rpc(method: str, params=None):
            return _rpc_call_abstract_socket(_ABSTRACT_SOCKET_NAME.lstrip("\x00"), method, params)
        return _abstract_rpc

    import urllib.request

    def _http_rpc(method: str, params=None):
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
        req = urllib.request.Request(
            f"{indexer_url}/rpc",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return json.loads(resp.read())

    return _http_rpc


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
    pytest.skip("Could not identify embedder PID")


@pytest.fixture(scope="session")
def indexer_pid(indexer_alive) -> int:
    """PID of the running indexer process."""
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        name = proc.info.get("name") or ""
        cmdline = " ".join(proc.info.get("cmdline") or [])
        if "opencode-indexer" in name or "opencode-indexer" in cmdline:
            return proc.info["pid"]
    pytest.skip("Could not identify indexer PID")


# ---------------------------------------------------------------------------
# In-process embedder server (no external dependency)
# ---------------------------------------------------------------------------

import asyncio
import sys
import threading


def _free_port() -> int:
    """Return 0 so the server binds to an OS-assigned port (no TOCTOU race)."""
    return 0


def _discover_listening_port(pid: int) -> int | None:
    """Read /proc/<pid>/net/tcp to find the embedder's listening port on 127.0.0.1."""
    try:
        with open(f"/proc/{pid}/net/tcp") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 2 and parts[1] != "local_address":
                    local_addr = parts[1]
                    if ":" in local_addr:
                        addr_hex, port_hex = local_addr.split(":")
                        if addr_hex == "0100007F":  # 127.0.0.1 in hex (big-endian: 7F000001)
                            return int(port_hex, 16)
    except (OSError, ValueError, IndexError):
        pass
    return None


@pytest.fixture(scope="session")
def inprocess_embedder_url():
    """Spin up a ModelServer in a background thread; yield its URL; then shut down.

    Uses the same pattern as test_server.py:
    - Patches _warmup_models to a no-op so startup is instant.
    - Runs the asyncio event loop in a daemon thread.
    """
    embedder_dir = str(Path(__file__).parent.parent / "embedder")
    if embedder_dir not in sys.path:
        sys.path.insert(0, embedder_dir)

    from opencode_embedder.server import ModelServer  # noqa: PLC0415

    # Patch warmup to no-op
    original_warmup = ModelServer._warmup_models

    async def noop_warmup(self):
        pass

    ModelServer._warmup_models = noop_warmup

    port = _free_port()  # returns 0 — OS assigns on bind
    os.environ["OPENCODE_EMBED_HTTP_PORT"] = str(port)
    os.environ.setdefault("OPENCODE_EMBED_IDLE_SHUTDOWN", "0")

    srv = ModelServer(embed_workers=1)
    loop = asyncio.new_event_loop()

    def run():
        asyncio.set_event_loop(loop)
        task = loop.create_task(srv.serve())
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    # Discover actual port from /proc/<pid>/net/tcp
    pid = os.getpid()
    actual_port = None
    for _ in range(200):
        time.sleep(0.1)
        actual_port = _discover_listening_port(pid)
        if actual_port is not None:
            break

    if actual_port is None:
        raise RuntimeError(
            f"Could not discover embedder listening port after 20s "
            f"(PID {pid}). Check that ModelServer.serve() started correctly."
        )

    url = f"http://127.0.0.1:{actual_port}"

    # Wait for health endpoint
    started = False
    for _ in range(100):
        time.sleep(0.1)
        if _check_url(f"{url}/health"):
            started = True
            break

    if not started:
        raise RuntimeError(
            f"Embedder server did not respond to health check at {url}/health "
            f"within 10 seconds. Thread alive: {thread.is_alive()}"
        )

    yield url

    # Shutdown
    srv._shutdown.set()
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    ModelServer._warmup_models = original_warmup
    os.environ.pop("OPENCODE_EMBED_HTTP_PORT", None)
