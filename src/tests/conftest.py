"""Test configuration for opencode-search package tests.

GPU enforcement rule: all tests run — no skips.
Tests that call actual GPU inference are marked @pytest.mark.gpu and run only
when a real CUDA device is present. All other tests (logic, config, storage,
chunking, search cache, handler routing) run via mocking and must always pass.

This conftest patches opencode_search.embeddings at session scope so that
modules importing it are importable even without a real GPU.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _gpu_available() -> bool:
    try:
        import onnxruntime as ort
        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:
        return False


HAS_GPU = _gpu_available()


def pytest_configure(config):
    config.addinivalue_line("markers", "gpu: test requires a real CUDA GPU")


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
                try:
                    __import__(mod_name)
                except Exception:
                    pass
        yield

    emb._provider_detection_done = _orig_done
    emb._detected_providers = _orig_providers
