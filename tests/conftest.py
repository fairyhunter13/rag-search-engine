"""Pytest fixtures for E2E resource efficiency tests.

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

import os
import time
from pathlib import Path

import psutil
import pytest

EMBEDDER_URL = os.environ.get("EMBEDDER_URL", "http://127.0.0.1:9998")
INDEXER_PORT_FILE = Path.home() / ".opencode" / "indexer.port"
EMBEDDER_TOKEN_FILE = Path.home() / ".opencode" / "embedder.token"


def _read_embedder_token() -> str | None:
    try:
        return EMBEDDER_TOKEN_FILE.read_text().strip()
    except FileNotFoundError:
        return None


def _read_indexer_port() -> int | None:
    try:
        return int(INDEXER_PORT_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _check_url(url: str, token: str | None = None, timeout: float = 3.0) -> bool:
    """Return True if the URL responds with HTTP 200."""
    import urllib.request
    try:
        req = urllib.request.Request(url)
        if token:
            req.add_header("X-Embedder-Token", token)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


@pytest.fixture(scope="session")
def embedder_token() -> str | None:
    return _read_embedder_token()


@pytest.fixture(scope="session")
def embedder_url() -> str:
    # Re-read at fixture time so EMBEDDER_URL env is resolved after arg processing
    return os.environ.get("EMBEDDER_URL", "http://127.0.0.1:9998")


@pytest.fixture(scope="session")
def indexer_url() -> str | None:
    port = _read_indexer_port()
    return f"http://127.0.0.1:{port}" if port else None


@pytest.fixture(scope="session")
def embedder_alive(embedder_url, embedder_token):
    """Skip if embedder not running."""
    if not _check_url(f"{embedder_url}/health", token=embedder_token):
        pytest.skip(f"Embedder not reachable at {embedder_url}/health")
    return True


@pytest.fixture(scope="session")
def indexer_alive(indexer_url):
    """Skip if indexer not running."""
    if indexer_url is None:
        pytest.skip("~/.opencode/indexer.port not found; indexer not running")
    if not _check_url(f"{indexer_url}/ping"):
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
import socket
import sys
import threading


def _free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


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

    port = _free_port()
    os.environ["OPENCODE_EMBED_HTTP_PORT"] = str(port)
    os.environ.setdefault("OPENCODE_EMBED_IDLE_SHUTDOWN", "0")

    srv = ModelServer(embed_workers=1)
    loop = asyncio.new_event_loop()
    url = f"http://127.0.0.1:{port}"

    def run():
        asyncio.set_event_loop(loop)
        task = loop.create_task(srv.serve())
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    # Wait for health endpoint
    for _ in range(100):
        time.sleep(0.1)
        if _check_url(f"{url}/health"):
            break

    yield url

    # Shutdown
    srv._shutdown.set()
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    ModelServer._warmup_models = original_warmup
    os.environ.pop("OPENCODE_EMBED_HTTP_PORT", None)
