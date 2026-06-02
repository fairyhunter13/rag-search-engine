"""E2E test configuration — fixtures for both pipeline tests and embedder/indexer service tests."""
from __future__ import annotations

import os

import pytest

# Re-export from root conftest so e2e pipeline tests can import directly
from tests.conftest import index_and_wait  # noqa: F401

from tests.e2e._infra import (
    EMBEDDER_URL,
    check_url,
    embedder_alive as _embedder_alive,
    find_embedder_pid as _find_embedder_pid,
    find_indexer_pid as _find_indexer_pid,
    read_indexer_port,
    check_abstract_socket,
    strict_no_skip,
)


@pytest.fixture(scope="session")
def embedder_url() -> str:
    return os.environ.get("EMBEDDER_URL", EMBEDDER_URL)


@pytest.fixture(scope="session")
def indexer_url() -> str | None:
    port = read_indexer_port()
    if port:
        return f"http://127.0.0.1:{port}"
    if check_abstract_socket():
        return "abstract://@opencode-indexer"
    return None


@pytest.fixture(scope="session")
def embedder_alive(embedder_url):
    if not check_url(f"{embedder_url}/health"):
        if strict_no_skip():
            pytest.fail(f"Embedder not reachable at {embedder_url}/health")
        pytest.skip(f"Embedder not reachable at {embedder_url}/health")
    return True


@pytest.fixture(scope="session")
def indexer_alive(indexer_url):
    if indexer_url is None:
        if strict_no_skip():
            pytest.fail("Indexer not running (no port file, no abstract socket)")
        pytest.skip("Indexer not running")
    if not indexer_url.startswith("abstract://") and not check_url(f"{indexer_url}/ping"):
        if strict_no_skip():
            pytest.fail(f"Indexer not reachable at {indexer_url}/ping")
        pytest.skip(f"Indexer not reachable at {indexer_url}/ping")
    return True


@pytest.fixture(scope="session")
def embedder_pid(embedder_alive) -> int:
    import psutil
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        cmdline = " ".join(proc.info.get("cmdline") or [])
        if "opencode_embedder" in cmdline or "opencode-embedder" in cmdline:
            return proc.info["pid"]
    if strict_no_skip():
        pytest.fail("Could not identify embedder PID")
    pytest.skip("Could not identify embedder PID")


@pytest.fixture(scope="session")
def indexer_pid(indexer_alive) -> int:
    import psutil
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        name = proc.info.get("name") or ""
        cmdline = " ".join(proc.info.get("cmdline") or [])
        if "opencode-indexer" in name or "opencode-indexer" in cmdline:
            return proc.info["pid"]
    if strict_no_skip():
        pytest.fail("Could not identify indexer PID")
    pytest.skip("Could not identify indexer PID")


def pytest_sessionfinish(session, exitstatus):
    if not strict_no_skip():
        return
    skipped = session.config.pluginmanager.get_plugin("terminalreporter").stats.get("skipped", [])
    if skipped and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
