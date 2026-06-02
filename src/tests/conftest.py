"""Test configuration for opencode-search package tests.

GPU enforcement rule: all tests run — no skips.
Tests that call actual GPU inference are marked @pytest.mark.gpu and run only
when a real CUDA device is present. All other tests (logic, config, storage,
chunking, search cache, handler routing) run via mocking and must always pass.
Tests marked @pytest.mark.integration may still skip if their runtime
dependencies are not installed in the current environment.

This conftest patches opencode_search.embeddings at session scope so that
modules importing it are importable even without a real GPU.
"""
from __future__ import annotations

import contextlib
import os
import sys
from unittest.mock import patch

import pytest


def _gpu_available() -> bool:
    # Use NVML (management-only) instead of ort.get_available_providers() to avoid
    # loading libonnxruntime_providers_cuda.so which initializes the CUDA UVM subsystem
    # in the test process.  On Blackwell (SM 12.0) two processes with active CUDA UVM
    # contexts cause cross-process UVM page-fault serialization that stalls indefinitely.
    # NVML only opens /dev/nvidiactl (management), never /dev/nvidia-uvm (compute).
    try:
        import pynvml
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        pynvml.nvmlShutdown()
        return count > 0
    except Exception:
        return False


HAS_GPU = _gpu_available()


def _strict_no_skip_enabled() -> bool:
    return os.environ.get("OPENCODE_FAIL_ON_SKIP", "").strip().lower() in {"1", "true", "yes", "on"}


def pytest_configure(config):
    config.addinivalue_line("markers", "gpu: test requires a real CUDA GPU")
    config.addinivalue_line("markers", "integration: test exercises real runtime integrations")
    config.addinivalue_line("markers", "runtime_deps: test requires optional runtime packages")
    config.addinivalue_line("markers", "large: test requires OPENCODE_RUN_LARGE_TESTS=1 and large external repos")
    config.addinivalue_line("markers", "unit: fast logic-only test")


def pytest_collection_modifyitems(config, items):
    """Skip @pytest.mark.gpu tests only when no GPU is present.
    All other tests MUST run — no unconditional skips allowed.
    """
    if HAS_GPU:
        return
    skip_gpu = pytest.mark.skip(reason="no CUDA GPU available on this machine")
    for item in items:
        if item.get_closest_marker("gpu"):
            item.add_marker(skip_gpu)


def pytest_sessionfinish(session, exitstatus):
    """Fail strict validation runs if pytest reported any skipped tests."""
    if not _strict_no_skip_enabled():
        return
    skipped = session.config.pluginmanager.get_plugin("terminalreporter").stats.get("skipped", [])
    if skipped and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def isolate_registry(tmp_path):
    """Redirect REGISTRY_PATH to a per-test temp file so tests never write to the real registry.

    Tests that explicitly set config.REGISTRY_PATH themselves via monkeypatch or patch()
    will override this at the narrower scope, which is fine — pytest fixture scoping
    ensures their more-specific patches take precedence.
    """
    import opencode_search.config as cfg

    tmp_registry = tmp_path / "projects.json"
    with patch.object(cfg, "REGISTRY_PATH", tmp_registry):
        yield tmp_registry


@pytest.fixture(scope="session", autouse=True)
def patch_embeddings_for_no_gpu():
    """Patch embeddings so opencode_search modules are importable without GPU.

    Patches _provider_detection_done + is_gpu_available so that:
     - Module-level GPU detection doesn't raise GPUNotAvailableError
     - Tests exercising logic/cache/routing still work without a real GPU
     - Tests that need actual GPU inference are @pytest.mark.gpu
    """
    if HAS_GPU:
        yield
        return

    import opencode_search.embeddings as emb

    _orig_done = emb._provider_detection_done
    _orig_providers = emb._detected_providers

    emb._provider_detection_done = True
    emb._detected_providers = ["CUDAExecutionProvider"]

    with patch.object(emb, "is_gpu_available", return_value=True), \
         patch.object(emb, "get_active_provider", return_value="cuda"), \
         patch.object(emb, "assert_gpu_available", return_value=None):
        # Force-import modules that run GPU checks at import time
        for mod_name in [
            "opencode_search.indexer",
            "opencode_search.mcp",
            "opencode_search.handlers",
            "opencode_search.search",
        ]:
            if mod_name not in sys.modules:
                with contextlib.suppress(Exception):
                    __import__(mod_name)
        yield

    emb._provider_detection_done = _orig_done
    emb._detected_providers = _orig_providers


# ---------------------------------------------------------------------------
# Async helpers for fire-and-forget index_project tests
# ---------------------------------------------------------------------------

async def index_and_wait(path: str, **kwargs) -> dict:
    """Call handle_index_project and wait for the background task to complete.

    handle_index_project now returns immediately with status='indexing'.
    This helper drains all pending asyncio tasks so tests can inspect the
    final outcome without polling.  Returns the final status dict from
    _indexing_status.
    """
    import asyncio
    from pathlib import Path as _Path

    from opencode_search.handlers import _indexing_status, handle_index_project

    result = await handle_index_project(path=path, **kwargs)
    # Drain the background task while the caller's patches are still active.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    path_str = str(_Path(path).expanduser().resolve())
    return _indexing_status.get(path_str, result)
