"""Test configuration and GPU-mock fixtures.

GPU enforcement rule: all tests run — no skips.
Tests that call actual GPU inference are marked @pytest.mark.gpu and run only
when a real CUDA device is present. All other tests (logic, structure, cache,
semaphore, normalization) run via mocking and must always pass.

This conftest patches opencode_embedder.embeddings at import time so that
server.py can be imported even in CI/dev environments without a GPU.
"""
from __future__ import annotations

import sys
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
        return  # GPU present → run everything
    skip_gpu = pytest.mark.skip(reason="no CUDA GPU available on this machine")
    for item in items:
        if item.get_closest_marker("gpu"):
            item.add_marker(skip_gpu)


@pytest.fixture(scope="session", autouse=True)
def patch_embeddings_for_no_gpu():
    """Patch provider detection so server.py is importable without a real GPU.

    Without a GPU, _detect_embed_workers() calls is_gpu_available() which
    triggers _get_onnx_providers() which raises GPUNotAvailableError — blocking
    any import of server.py.

    This session-scoped patch pretends CUDA is available so module-level code
    runs. Tests that actually exercise GPU inference are @pytest.mark.gpu and
    only run when a real GPU is present.
    """
    if HAS_GPU:
        yield  # no patch needed
        return

    # Pre-populate the provider cache before any module import
    import opencode_embedder.embeddings as emb

    _orig_done = emb._provider_detection_done
    _orig_providers = emb._detected_providers

    emb._provider_detection_done = True
    emb._detected_providers = ["CUDAExecutionProvider"]

    # Also patch is_gpu_available to return True so downstream checks pass
    with patch.object(emb, "is_gpu_available", return_value=True), \
         patch.object(emb, "get_active_provider", return_value="cuda"):
        # Force-import server.py now while patches are active
        if "opencode_embedder.server" not in sys.modules:
            try:
                import opencode_embedder.server  # noqa: F401
            except Exception:
                pass  # If it still fails, individual tests will handle it
        yield

    # Restore original state
    emb._provider_detection_done = _orig_done
    emb._detected_providers = _orig_providers
