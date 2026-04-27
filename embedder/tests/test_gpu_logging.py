"""Tests for GPU logging and provider detection functions."""

import logging


def test_get_gpu_stats_returns_dict():
    """Test that get_gpu_stats returns a dict with expected keys."""
    from opencode_embedder.embeddings import get_gpu_stats

    stats = get_gpu_stats()

    assert isinstance(stats, dict)
    assert "gpu_ops" in stats
    assert "cpu_ops" in stats
    assert "provider" in stats
    assert "is_gpu" in stats


def test_get_gpu_stats_types():
    """Test that get_gpu_stats returns correct types."""
    from opencode_embedder.embeddings import get_gpu_stats

    stats = get_gpu_stats()

    assert isinstance(stats["gpu_ops"], int)
    assert isinstance(stats["cpu_ops"], int)
    assert isinstance(stats["provider"], str)
    assert isinstance(stats["is_gpu"], bool)


def test_get_gpu_stats_non_negative_counts():
    """Test that operation counts are non-negative."""
    from opencode_embedder.embeddings import get_gpu_stats

    stats = get_gpu_stats()

    assert stats["gpu_ops"] >= 0
    assert stats["cpu_ops"] >= 0


def test_get_active_provider_returns_string():
    """Test that get_active_provider returns a valid provider string."""
    from opencode_embedder.embeddings import get_active_provider

    provider = get_active_provider()

    assert isinstance(provider, str)
    valid_providers = {"tensorrt", "cuda", "rocm", "directml", "migraphx", "coreml", "cpu"}
    assert provider in valid_providers, f"Unknown provider: {provider}"


def test_is_gpu_available_returns_bool():
    """Test that is_gpu_available returns a boolean."""
    from opencode_embedder.embeddings import is_gpu_available

    result = is_gpu_available()

    assert isinstance(result, bool)


def test_provider_consistency():
    """Test that provider detection is consistent with GPU availability."""
    from opencode_embedder.embeddings import get_active_provider, is_gpu_available

    provider = get_active_provider()
    is_gpu = is_gpu_available()

    gpu_providers = {"tensorrt", "cuda", "rocm", "directml", "migraphx", "coreml"}

    if is_gpu:
        assert provider in gpu_providers, f"GPU available but provider is {provider}"
    else:
        assert provider == "cpu", f"No GPU but provider is {provider}"


def test_embed_passages_increments_counter():
    """Test that embed_passages increments the operation counter."""
    from opencode_embedder.embeddings import embed_passages, get_gpu_stats

    # Get initial counts
    before = get_gpu_stats()
    initial_gpu = before["gpu_ops"]
    initial_cpu = before["cpu_ops"]

    # Run embedding
    embed_passages(
        ["test text"],
        model="jinaai/jina-embeddings-v2-small-en",
        dimensions=512,
    )

    # Get final counts
    after = get_gpu_stats()

    # Either GPU or CPU count should have increased by 1
    gpu_delta = after["gpu_ops"] - initial_gpu
    cpu_delta = after["cpu_ops"] - initial_cpu

    assert gpu_delta + cpu_delta == 1, (
        f"Expected exactly one op increment, got gpu_delta={gpu_delta}, cpu_delta={cpu_delta}"
    )


def test_embed_query_increments_counter():
    """Test that embed_query increments the operation counter."""
    from opencode_embedder.embeddings import embed_query, get_gpu_stats

    # Get initial counts
    before = get_gpu_stats()
    initial_gpu = before["gpu_ops"]
    initial_cpu = before["cpu_ops"]

    # Run embedding
    embed_query(
        "test query",
        model="jinaai/jina-embeddings-v2-small-en",
        dimensions=512,
    )

    # Get final counts
    after = get_gpu_stats()

    # Either GPU or CPU count should have increased by 1
    gpu_delta = after["gpu_ops"] - initial_gpu
    cpu_delta = after["cpu_ops"] - initial_cpu

    assert gpu_delta + cpu_delta == 1, (
        f"Expected exactly one op increment, got gpu_delta={gpu_delta}, cpu_delta={cpu_delta}"
    )


def test_embed_passages_logs_provider(caplog):
    """Test that embed_passages logs the provider being used."""
    from opencode_embedder.embeddings import embed_passages, get_active_provider

    provider = get_active_provider()

    with caplog.at_level(logging.INFO, logger="opencode_embedder.embeddings"):
        embed_passages(
            ["test logging"],
            model="jinaai/jina-embeddings-v2-small-en",
            dimensions=512,
        )

    # Check that provider is mentioned in logs
    log_text = caplog.text.upper()
    assert provider.upper() in log_text, f"Provider {provider} not found in logs: {caplog.text}"


def test_verify_onnx_session_provider_logs(caplog):
    """Test that _verify_onnx_session_provider logs session info on fresh load."""
    from opencode_embedder import embeddings

    # Clear cached embedder to force a fresh load
    embeddings._cached_embedder = None
    embeddings._cached_embedder_model = None

    with caplog.at_level(logging.INFO, logger="opencode_embedder.embeddings"):
        # Force fresh model load to trigger verification
        embeddings._embedder("jinaai/jina-embeddings-v2-small-en")

    # Should have logged something about ONNX session or provider
    log_text = caplog.text.lower()
    # Either logs active providers or a warning about access
    assert "onnx" in log_text or "provider" in log_text or "loading" in log_text, (
        f"Expected ONNX/provider/loading info in logs: {caplog.text}"
    )


def test_health_endpoint_includes_gpu_info():
    """Test that health endpoint includes GPU information."""
    from opencode_embedder.server import ModelServer

    server = ModelServer()
    health = server._handle_health()

    assert "gpu" in health
    assert "provider" in health["gpu"]
    assert "is_gpu" in health["gpu"]
    assert "gpu_ops" in health["gpu"]
    assert "cpu_ops" in health["gpu"]


def test_health_endpoint_gpu_types():
    """Test that health endpoint GPU info has correct types."""
    from opencode_embedder.server import ModelServer

    server = ModelServer()
    health = server._handle_health()
    gpu = health["gpu"]

    assert isinstance(gpu["provider"], str)
    assert isinstance(gpu["is_gpu"], bool)
    assert isinstance(gpu["gpu_ops"], int)
    assert isinstance(gpu["cpu_ops"], int)


def test_onnx_providers_detection():
    """Test that ONNX provider detection works."""
    from opencode_embedder.embeddings import _get_onnx_providers

    providers = _get_onnx_providers()

    # Should return either None (defaults) or a list of strings
    if providers is not None:
        assert isinstance(providers, list)
        assert all(isinstance(p, str) for p in providers)
        # GPU-only: no CPUExecutionProvider in provider list
        assert "CPUExecutionProvider" not in providers


def test_provider_env_override(monkeypatch):
    """Test that OPENCODE_ONNX_PROVIDER env var works."""
    from opencode_embedder import embeddings

    # Reset cached detection
    embeddings._detected_providers = None
    embeddings._provider_detection_done = False

    # "cpu" is no longer in the provider_map (GPU-only enforcement).
    # Unknown shortcuts fall through to auto-detection.
    monkeypatch.setenv("OPENCODE_ONNX_PROVIDER", "cpu")
    monkeypatch.delenv("OPENCODE_GPU_REQUIRED", raising=False)

    try:
        providers = embeddings._get_onnx_providers()
    except RuntimeError:
        providers = None  # no GPU in test env

    # Falls through to auto-detection: returns GPU providers (not CPU)
    assert providers is None or "CPUExecutionProvider" not in (providers or [])

    # Clean up
    embeddings._detected_providers = None
    embeddings._provider_detection_done = False


def test_provider_env_override_invalid(monkeypatch, caplog):
    """Test that invalid OPENCODE_ONNX_PROVIDER logs warning and falls through to detection."""
    from opencode_embedder import embeddings

    # Reset cached detection
    embeddings._detected_providers = None
    embeddings._provider_detection_done = False

    # Set invalid env var; disable GPU_REQUIRED so auto-detection can proceed without raising
    monkeypatch.setenv("OPENCODE_ONNX_PROVIDER", "invalid_provider")
    monkeypatch.delenv("OPENCODE_GPU_REQUIRED", raising=False)

    with caplog.at_level(logging.WARNING, logger="opencode_embedder.embeddings"):
        try:
            embeddings._get_onnx_providers()
        except RuntimeError:
            pass  # GPU enforcement may raise on CI without GPU

    assert "unknown" in caplog.text.lower() or "invalid" in caplog.text.lower()

    # Clean up
    embeddings._detected_providers = None
    embeddings._provider_detection_done = False


# ---------------------------------------------------------------------------
# TensorRT provider tests
# ---------------------------------------------------------------------------


def test_tensorrt_in_valid_providers():
    """Test that tensorrt is a recognised GPU provider string."""
    from opencode_embedder.embeddings import get_active_provider, is_gpu_available

    provider = get_active_provider()

    if provider == "tensorrt":
        assert is_gpu_available(), "TensorRT provider must report GPU available"


def test_tensorrt_provider_env_override(monkeypatch):
    """Test that OPENCODE_ONNX_PROVIDER=tensorrt sets TensorRT+CUDA+CPU providers."""
    from opencode_embedder import embeddings

    embeddings._detected_providers = None
    embeddings._provider_detection_done = False

    monkeypatch.setenv("OPENCODE_ONNX_PROVIDER", "tensorrt")

    providers = embeddings._get_onnx_providers()

    assert providers is not None
    assert "TensorrtExecutionProvider" in providers
    assert "CUDAExecutionProvider" in providers
    assert "CPUExecutionProvider" not in providers  # GPU-only: no CPU fallback
    # TensorRT must be first (highest priority)
    assert providers[0] == "TensorrtExecutionProvider"

    embeddings._detected_providers = None
    embeddings._provider_detection_done = False


def test_tensorrt_provider_env_override_logs(monkeypatch, caplog):
    """Test that OPENCODE_ONNX_PROVIDER=tensorrt is mentioned in logs."""
    from opencode_embedder import embeddings

    embeddings._detected_providers = None
    embeddings._provider_detection_done = False

    monkeypatch.setenv("OPENCODE_ONNX_PROVIDER", "tensorrt")

    with caplog.at_level(logging.INFO, logger="opencode_embedder.embeddings"):
        embeddings._get_onnx_providers()

    assert "tensorrt" in caplog.text.lower(), f"Expected 'tensorrt' in logs: {caplog.text}"

    embeddings._detected_providers = None
    embeddings._provider_detection_done = False


def test_tensorrt_is_gpu_provider():
    """Test that is_gpu_available returns True when TensorRT is in the provider list."""
    from opencode_embedder import embeddings

    original = embeddings._get_onnx_providers
    embeddings._get_onnx_providers = lambda: [
        "TensorrtExecutionProvider",
        "CUDAExecutionProvider",
    ]

    result = embeddings.is_gpu_available()

    embeddings._get_onnx_providers = original

    assert result is True
