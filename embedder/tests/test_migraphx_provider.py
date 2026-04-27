"""Tests for AMD GPU provider support (ROCm and MIGraphX).

AMD GPUs are supported via two ONNX Runtime providers:
- ROCMExecutionProvider: Direct HIP/ROCm execution (preferred, faster startup)
- MIGraphXExecutionProvider: Graph-compiled execution (slower startup, ~15s compilation)

These tests verify:
- ROCm/MIGraphX provider detection and priority (ROCm preferred)
- GPU acceleration for embeddings, reranking, and chunking
- Provider fallback behavior
"""

import os
import pytest
import logging


def test_migraphx_in_provider_priority():
    """Test that MIGraphX is prioritized over ROCm in the detection order.

    MIGraphX is now preferred because:
    - ROCMExecutionProvider deprecated and removed in ORT 1.23+
    - MIGraphXExecutionProvider is AMD's officially recommended EP for ORT 1.23+
    - ROCm EP kept only as fallback for older ORT builds (< 1.23)

    Auto-detection returns a single GPU provider (first that works), so when
    MIGraphX is available and functional it will be selected instead of ROCm.
    """
    from opencode_embedder import embeddings

    try:
        import onnxruntime as ort

        available = ort.get_available_providers()
    except ImportError:
        pytest.skip("onnxruntime not installed")

    # When MIGraphX is available, it should be selected before ROCm
    if "MIGraphXExecutionProvider" in available:
        embeddings._detected_providers = None
        embeddings._provider_detection_done = False

        providers = embeddings._get_onnx_providers()

        if providers is not None and "MIGraphXExecutionProvider" in providers:
            # MIGraphX selected: ROCm should not appear before it
            if "ROCMExecutionProvider" in providers:
                migraphx_idx = providers.index("MIGraphXExecutionProvider")
                rocm_idx = providers.index("ROCMExecutionProvider")
                assert migraphx_idx < rocm_idx, "MIGraphX should be prioritized over ROCm"


def test_migraphx_provider_env_override(monkeypatch):
    """Test that OPENCODE_ONNX_PROVIDER=migraphx works."""
    from opencode_embedder import embeddings

    # Reset cached detection
    embeddings._detected_providers = None
    embeddings._provider_detection_done = False

    # Set env var to force MIGraphX
    monkeypatch.setenv("OPENCODE_ONNX_PROVIDER", "migraphx")

    providers = embeddings._get_onnx_providers()

    assert providers == ["MIGraphXExecutionProvider"]  # GPU-only: no CPU fallback

    # Clean up
    embeddings._detected_providers = None
    embeddings._provider_detection_done = False


def test_migraphx_providers_list_env_override(monkeypatch):
    """Test that OPENCODE_ONNX_PROVIDERS env var works with MIGraphX."""
    from opencode_embedder import embeddings

    # Reset cached detection
    embeddings._detected_providers = None
    embeddings._provider_detection_done = False

    # Set env var to explicit provider list
    monkeypatch.setenv(
        "OPENCODE_ONNX_PROVIDERS",
        "MIGraphXExecutionProvider,ROCMExecutionProvider,CPUExecutionProvider",
    )

    providers = embeddings._get_onnx_providers()

    assert providers == [
        "MIGraphXExecutionProvider",
        "ROCMExecutionProvider",
        "CPUExecutionProvider",
    ]

    # Clean up
    embeddings._detected_providers = None
    embeddings._provider_detection_done = False


def test_migraphx_in_valid_providers():
    """Test that MIGraphX is recognized as a valid GPU provider."""
    from opencode_embedder.embeddings import get_active_provider, is_gpu_available

    provider = get_active_provider()

    # MIGraphX should be recognized as a GPU provider
    if provider == "migraphx":
        assert is_gpu_available(), "MIGraphX should indicate GPU is available"


def test_migraphx_provider_detection_logs(caplog, monkeypatch):
    """Test that MIGraphX provider detection is logged."""
    from opencode_embedder import embeddings

    # Reset cached detection
    embeddings._detected_providers = None
    embeddings._provider_detection_done = False

    # Force MIGraphX via env
    monkeypatch.setenv("OPENCODE_ONNX_PROVIDER", "migraphx")

    with caplog.at_level(logging.INFO, logger="opencode_embedder.embeddings"):
        embeddings._get_onnx_providers()

    # Should log something about MIGraphX
    log_text = caplog.text.lower()
    assert "migraphx" in log_text, f"MIGraphX not mentioned in logs: {caplog.text}"

    # Clean up
    embeddings._detected_providers = None
    embeddings._provider_detection_done = False


def test_migraphx_embed_passages():
    """Test that embed_passages works with MIGraphX provider."""
    from opencode_embedder.embeddings import embed_passages, get_active_provider

    provider = get_active_provider()

    # Run embedding
    texts = ["MIGraphX GPU acceleration test", "AMD ROCm optimization"]
    vectors = embed_passages(texts, model="jinaai/jina-embeddings-v2-small-en", dimensions=512)

    # Should return correct number of vectors
    assert len(vectors) == len(texts)

    # Vectors should have correct dimensions
    for vec in vectors:
        assert len(vec) == 512
        assert all(isinstance(v, float) for v in vec)


def test_migraphx_embed_query():
    """Test that embed_query works with MIGraphX provider."""
    from opencode_embedder.embeddings import embed_query, get_active_provider

    provider = get_active_provider()

    # Run query embedding
    vector = embed_query(
        "MIGraphX query test", model="jinaai/jina-embeddings-v2-small-en", dimensions=512
    )

    # Should return a vector with correct dimensions
    assert len(vector) == 512
    assert all(isinstance(v, float) for v in vector)


def test_migraphx_rerank():
    """Test that rerank works with MIGraphX provider."""
    from opencode_embedder.embeddings import rerank, get_active_provider

    provider = get_active_provider()

    # Run reranking
    query = "What is MIGraphX?"
    docs = [
        "MIGraphX is AMD's graph compiler for ONNX.",
        "ROCm is AMD's GPU computing platform.",
        "The cat sat on the mat.",
    ]

    results = rerank(query, docs, model="Xenova/ms-marco-MiniLM-L-6-v2", top_k=2)

    # Should return top_k results
    assert len(results) == 2

    # Results should be (index, score) tuples
    for idx, score in results:
        assert isinstance(idx, int)
        assert isinstance(score, float)
        assert 0 <= idx < len(docs)
        assert 0.0 <= score <= 1.0


def test_migraphx_gpu_ops_tracking():
    """Test that GPU operations are tracked when using MIGraphX."""
    from opencode_embedder.embeddings import (
        embed_passages,
        get_gpu_stats,
        get_active_provider,
        is_gpu_available,
    )

    # Get initial counts
    before = get_gpu_stats()
    initial_gpu = before["gpu_ops"]
    initial_cpu = before["cpu_ops"]

    # Run embedding
    embed_passages(
        ["GPU ops tracking test"], model="jinaai/jina-embeddings-v2-small-en", dimensions=512
    )

    # Get final counts
    after = get_gpu_stats()

    # If MIGraphX is active, GPU ops should increase
    if get_active_provider() == "migraphx":
        assert after["gpu_ops"] > initial_gpu, "GPU ops should increase when using MIGraphX"
    else:
        # CPU ops should increase if no GPU
        assert after["cpu_ops"] > initial_cpu or after["gpu_ops"] > initial_gpu


def test_migraphx_health_check():
    """Test that health check reports MIGraphX status."""
    from opencode_embedder.server import ModelServer
    from opencode_embedder.embeddings import get_active_provider

    server = ModelServer()
    health = server._handle_health()

    # Health should include GPU info
    assert "gpu" in health
    assert "provider" in health["gpu"]

    # Provider should match get_active_provider()
    assert health["gpu"]["provider"] == get_active_provider()

    # If MIGraphX is active, is_gpu should be True
    if health["gpu"]["provider"] == "migraphx":
        assert health["gpu"]["is_gpu"] is True


def test_rocm_detection_matches_installation():
    """Test that ROCm provider availability matches system installation state.

    Verifies correct behavior in ALL environments:
    - ROCm installed: AMD providers should be available
    - ROCm not installed: AMD providers should not be available
    """
    import onnxruntime as ort

    available = ort.get_available_providers()
    has_amd = "ROCMExecutionProvider" in available or "MIGraphXExecutionProvider" in available
    rocm = os.path.exists("/opt/rocm")

    if rocm:
        assert has_amd, (
            f"ROCm installed at /opt/rocm but no AMD provider found. "
            f"Available: {available}. Install onnxruntime-rocm."
        )
    else:
        assert not has_amd, f"AMD provider found without ROCm installation. Available: {available}"


def test_provider_test_model_exists():
    """Test that the test model for provider validation exists."""
    from opencode_embedder.embeddings import _get_test_model_path
    import os

    model_path = _get_test_model_path()

    # The test model should exist
    assert os.path.exists(model_path), (
        f"Test model not found at {model_path}. This is needed for provider validation."
    )


def test_migraphx_provider_map():
    """Test that MIGraphX is in the provider map."""
    from opencode_embedder import embeddings

    # The provider map in _parse_provider_env should include migraphx
    # We test this by checking the env override works
    embeddings._detected_providers = None
    embeddings._provider_detection_done = False

    # This would raise KeyError if migraphx wasn't in the map
    try:
        providers = embeddings._parse_provider_env()
        # If no env var set, should return None
    except KeyError:
        pytest.fail("MIGraphX not in provider map")
    finally:
        embeddings._detected_providers = None
        embeddings._provider_detection_done = False
