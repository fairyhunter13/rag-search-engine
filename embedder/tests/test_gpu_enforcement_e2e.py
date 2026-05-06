"""E2E test: assert GPU is actually being used for inference.

Queries the running embedder's health endpoint (HTTP only — no direct module imports).
Skipped automatically when:
  - embedder is not running at EMBEDDER_URL (or http://127.0.0.1:9998)
  - OPENCODE_ONNX_PROVIDER=cpu (intentional CPU mode)
"""

import os
import urllib.error
import urllib.request
import json
import pytest

EMBEDDER_URL = os.environ.get("EMBEDDER_URL", "http://127.0.0.1:9998")
GPU_PROVIDERS = {"tensorrt", "cuda", "migraphx", "rocm"}


def _cpu_mode() -> bool:
    return os.environ.get("OPENCODE_ONNX_PROVIDER", "").lower() == "cpu"


def _fetch_health() -> dict:
    """Fetch /health from the running embedder. Raises urllib.error.URLError if not running.

    Unwraps {"result": {...}} envelope if present.
    """
    url = f"{EMBEDDER_URL.rstrip('/')}/health"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read())
    # Unwrap {"result": {...}} envelope used by this server
    if "result" in data and isinstance(data["result"], dict):
        return data["result"]
    return data


def _get_health_or_skip() -> dict:
    """Return health dict or call pytest.skip if embedder is not reachable."""
    try:
        return _fetch_health()
    except (urllib.error.URLError, OSError) as exc:
        pytest.skip(f"Embedder not running at {EMBEDDER_URL}: {exc}")


@pytest.mark.skipif(_cpu_mode(), reason="OPENCODE_ONNX_PROVIDER=cpu — intentional CPU mode")
def test_gpu_enforcement():
    """Health endpoint must report an active GPU provider."""
    health = _get_health_or_skip()
    gpu = health.get("gpu", {})
    provider = gpu.get("provider", "unknown")
    is_gpu = gpu.get("is_gpu", False)
    degraded = gpu.get("degraded", False)

    assert is_gpu, (
        f"GPU enforcement failed: is_gpu={is_gpu!r}  provider={provider!r}\n"
        f"  → GPU inference is required in production.\n"
        f"  → Set OPENCODE_ONNX_PROVIDER=cpu to skip this check intentionally.\n"
        f"  → Full gpu stats: {gpu}"
    )

    assert provider in GPU_PROVIDERS, (
        f"GPU enforcement failed: provider={provider!r} is not a recognised GPU provider.\n"
        f"  → Expected one of: {sorted(GPU_PROVIDERS)}\n"
        f"  → Full gpu stats: {gpu}"
    )

    assert not degraded, (
        f"GPU degraded: provider={provider!r} fell back to CPU (driver/library mismatch).\n"
        f"  → Check ONNX Runtime GPU shared libraries and CUDA/ROCm driver versions.\n"
        f"  → Full gpu stats: {gpu}"
    )


@pytest.mark.skipif(_cpu_mode(), reason="OPENCODE_ONNX_PROVIDER=cpu — intentional CPU mode")
def test_gpu_provider_reported_consistently():
    """Health endpoint gpu fields must be internally consistent."""
    health = _get_health_or_skip()
    gpu = health.get("gpu", {})

    is_gpu = gpu.get("is_gpu", False)
    provider = gpu.get("provider", "unknown")

    # If is_gpu is True, provider must be in the known GPU set
    if is_gpu:
        assert provider in GPU_PROVIDERS, (
            f"Health reports is_gpu=True but provider={provider!r} is not a GPU provider.\n"
            f"  → Full gpu stats: {gpu}"
        )
    else:
        assert provider not in GPU_PROVIDERS, (
            f"Health reports is_gpu=False but provider={provider!r} looks like a GPU provider.\n"
            f"  → Full gpu stats: {gpu}"
        )


def test_gpu_provider_is_not_cpu():
    """In normal (non-CPU-override) mode, provider must not be cpu."""
    if _cpu_mode():
        pytest.skip("OPENCODE_ONNX_PROVIDER=cpu — intentional CPU mode, skip GPU check")

    health = _get_health_or_skip()
    gpu = health.get("gpu", {})
    provider = gpu.get("provider", "unknown")

    assert provider != "cpu", (
        f"Provider is cpu in non-CPU-override mode: provider={provider!r}\n"
        f"  → GPU should be active. Check ONNX Runtime GPU libraries and CUDA drivers.\n"
        f"  → Full gpu stats: {gpu}"
    )
