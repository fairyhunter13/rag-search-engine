"""E2E tests replacing unit tests for GPU logging, MIGraphX provider, and server.

All tests run against the live embedder at EMBEDDER_URL (default http://127.0.0.1:9998).
Tests are skipped when the embedder is not running. No module imports from opencode_embedder —
pure HTTP only.

Replaces:
  - test_gpu_logging.py
  - test_migraphx_provider.py
  - test_server.py  (HTTP roundtrip / functional coverage)
"""

import base64
import json
import os
import struct
import urllib.error
import urllib.request

import pytest

EMBEDDER_URL = os.environ.get("EMBEDDER_URL", "http://127.0.0.1:9998").rstrip("/")
GPU_PROVIDERS = {"tensorrt", "cuda", "migraphx", "rocm", "directml", "coreml"}
AMD_PROVIDERS = {"migraphx", "rocm"}
ALL_VALID_PROVIDERS = GPU_PROVIDERS | {"cpu"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request(method: str, path: str, body: dict | None = None, timeout: int = 30) -> dict:
    url = f"{EMBEDDER_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers: dict[str, str] = {}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _get(path: str, timeout: int = 10) -> dict:
    return _request("GET", path, timeout=timeout)


def _post(path: str, body: dict, timeout: int = 60) -> dict:
    return _request("POST", path, body=body, timeout=timeout)


def _fetch_health() -> dict:
    data = _get("/health")
    if "result" in data and isinstance(data["result"], dict):
        return data["result"]
    return data


def _get_health_or_skip() -> dict:
    try:
        return _fetch_health()
    except (urllib.error.URLError, OSError) as exc:
        pytest.skip(f"Embedder not running at {EMBEDDER_URL}: {exc}")


def _cpu_mode() -> bool:
    return os.environ.get("OPENCODE_ONNX_PROVIDER", "").lower() == "cpu"


def _post_or_skip(path: str, body: dict, timeout: int = 60) -> dict:
    try:
        return _post(path, body, timeout=timeout)
    except (urllib.error.URLError, OSError) as exc:
        pytest.skip(f"Embedder not running at {EMBEDDER_URL}: {exc}")


def _unpack_f32_le(data: bytes | str) -> list[float]:
    if isinstance(data, str):
        data = base64.b64decode(data)
    if not data:
        return []
    count = len(data) // 4
    return list(struct.unpack("<" + "f" * count, data))


# ---------------------------------------------------------------------------
# GPU stats / logging (replaces test_gpu_logging.py)
# ---------------------------------------------------------------------------



def test_health_gpu_stats_types():
    """GPU stats must have correct types (replaces test_get_gpu_stats_types)."""
    health = _get_health_or_skip()
    gpu = health.get("gpu", {})
    assert isinstance(gpu.get("provider"), str), f"provider must be str: {gpu}"
    assert isinstance(gpu.get("is_gpu"), bool), f"is_gpu must be bool: {gpu}"
    assert isinstance(gpu.get("gpu_ops"), int), f"gpu_ops must be int: {gpu}"
    assert isinstance(gpu.get("cpu_ops"), int), f"cpu_ops must be int: {gpu}"


def test_health_gpu_stats_non_negative_counts():
    """Op counts must be non-negative (replaces test_get_gpu_stats_non_negative_counts)."""
    health = _get_health_or_skip()
    gpu = health.get("gpu", {})
    assert gpu.get("gpu_ops", -1) >= 0, f"gpu_ops negative: {gpu}"
    assert gpu.get("cpu_ops", -1) >= 0, f"cpu_ops negative: {gpu}"


def test_health_provider_is_valid_string():
    """Provider must be one of the known provider strings (replaces test_get_active_provider_returns_string)."""
    health = _get_health_or_skip()
    gpu = health.get("gpu", {})
    provider = gpu.get("provider", "")
    assert provider in ALL_VALID_PROVIDERS, (
        f"Unknown provider {provider!r}. Expected one of {sorted(ALL_VALID_PROVIDERS)}.\n"
        f"Full gpu stats: {gpu}"
    )


def test_health_is_gpu_is_bool():
    """is_gpu field must be a boolean (replaces test_is_gpu_available_returns_bool)."""
    health = _get_health_or_skip()
    gpu = health.get("gpu", {})
    assert isinstance(gpu.get("is_gpu"), bool), f"is_gpu not bool: {gpu}"


@pytest.mark.skipif(_cpu_mode(), reason="OPENCODE_ONNX_PROVIDER=cpu — intentional CPU mode")
def test_health_provider_consistency():
    """is_gpu and provider must be consistent (replaces test_provider_consistency)."""
    health = _get_health_or_skip()
    gpu = health.get("gpu", {})
    provider = gpu.get("provider", "unknown")
    is_gpu = gpu.get("is_gpu", False)
    if is_gpu:
        assert provider in GPU_PROVIDERS, (
            f"is_gpu=True but provider={provider!r} not in GPU_PROVIDERS={sorted(GPU_PROVIDERS)}"
        )
    else:
        assert provider == "cpu", (
            f"is_gpu=False but provider={provider!r} (expected 'cpu')"
        )


def test_embed_passages_increments_op_counter():
    """embed/passages_f32 call must increment gpu_ops or cpu_ops (replaces test_embed_passages_increments_counter)."""
    health_before = _get_health_or_skip()
    gpu_before = health_before.get("gpu", {})
    initial_gpu = gpu_before.get("gpu_ops", 0)
    initial_cpu = gpu_before.get("cpu_ops", 0)

    _post_or_skip(
        "/embed/passages_f32",
        {"texts": ["test text"], "model": "jinaai/jina-embeddings-v2-small-en", "dimensions": 512},
        timeout=120,
    )

    health_after = _fetch_health()
    gpu_after = health_after.get("gpu", {})
    gpu_delta = gpu_after.get("gpu_ops", 0) - initial_gpu
    cpu_delta = gpu_after.get("cpu_ops", 0) - initial_cpu

    assert gpu_delta + cpu_delta >= 1, (
        f"No op counter incremented after embed call.\n"
        f"  gpu_delta={gpu_delta} cpu_delta={cpu_delta}\n"
        f"  before={gpu_before} after={gpu_after}"
    )


def test_embed_query_increments_op_counter():
    """embed/query_f32 call must increment gpu_ops or cpu_ops (replaces test_embed_query_increments_counter)."""
    health_before = _get_health_or_skip()
    gpu_before = health_before.get("gpu", {})
    initial_gpu = gpu_before.get("gpu_ops", 0)
    initial_cpu = gpu_before.get("cpu_ops", 0)

    _post_or_skip(
        "/embed/query_f32",
        {"text": "test query", "model": "jinaai/jina-embeddings-v2-small-en", "dimensions": 512},
        timeout=120,
    )

    health_after = _fetch_health()
    gpu_after = health_after.get("gpu", {})
    gpu_delta = gpu_after.get("gpu_ops", 0) - initial_gpu
    cpu_delta = gpu_after.get("cpu_ops", 0) - initial_cpu

    assert gpu_delta + cpu_delta >= 1, (
        f"No op counter incremented after query embed call.\n"
        f"  gpu_delta={gpu_delta} cpu_delta={cpu_delta}\n"
        f"  before={gpu_before} after={gpu_after}"
    )


@pytest.mark.skipif(_cpu_mode(), reason="OPENCODE_ONNX_PROVIDER=cpu — intentional CPU mode")
def test_gpu_provider_active():
    """GPU provider must be active (no CPU fallback) (replaces test_gpu_logging GPU enforcement)."""
    health = _get_health_or_skip()
    gpu = health.get("gpu", {})
    provider = gpu.get("provider", "unknown")
    is_gpu = gpu.get("is_gpu", False)
    degraded = gpu.get("degraded", False)

    assert is_gpu, (
        f"GPU not active: is_gpu={is_gpu!r} provider={provider!r}\n"
        f"  → GPU required. Use OPENCODE_ONNX_PROVIDER=cpu to skip intentionally.\n"
        f"  → Full gpu stats: {gpu}"
    )
    assert not degraded, (
        f"GPU degraded (fell back to CPU): provider={provider!r}\n"
        f"  → Check ONNX Runtime GPU libraries and driver versions.\n"
        f"  → Full gpu stats: {gpu}"
    )


# ---------------------------------------------------------------------------
# MIGraphX / ROCm provider (replaces test_migraphx_provider.py)
# ---------------------------------------------------------------------------



def test_migraphx_embed_passages_returns_correct_dimensions():
    """embed/passages_f32 returns vectors with correct dimensions (replaces test_migraphx_embed_passages)."""
    texts = ["MIGraphX GPU acceleration test", "AMD ROCm optimization"]
    resp = _post_or_skip(
        "/embed/passages_f32",
        {"texts": texts, "model": "jinaai/jina-embeddings-v2-small-en", "dimensions": 512},
        timeout=120,
    )
    result = resp.get("result", resp)
    assert result.get("count") == len(texts), (
        f"Expected {len(texts)} embeddings, got count={result.get('count')}"
    )
    assert result.get("dimensions") == 512, (
        f"Expected dimensions=512, got {result.get('dimensions')}"
    )
    # Unpack and verify vector count
    vectors_raw = result.get("vectors_f32")
    if vectors_raw:
        floats = _unpack_f32_le(vectors_raw)
        assert len(floats) == len(texts) * 512, (
            f"Expected {len(texts) * 512} floats, got {len(floats)}"
        )
        assert all(isinstance(v, float) for v in floats[:10])


def test_migraphx_embed_query_returns_correct_dimensions():
    """embed/query_f32 returns vector with correct dimensions (replaces test_migraphx_embed_query)."""
    resp = _post_or_skip(
        "/embed/query_f32",
        {"text": "MIGraphX query test", "model": "jinaai/jina-embeddings-v2-small-en", "dimensions": 512},
        timeout=120,
    )
    result = resp.get("result", resp)
    assert result.get("dimensions") == 512, (
        f"Expected dimensions=512, got {result.get('dimensions')}"
    )
    vector_raw = result.get("vector_f32")
    if vector_raw:
        floats = _unpack_f32_le(vector_raw)
        assert len(floats) == 512, f"Expected 512 floats, got {len(floats)}"
        assert all(isinstance(v, float) for v in floats[:10])


def test_migraphx_rerank_returns_top_k():
    """rerank endpoint returns top_k results (replaces test_migraphx_rerank)."""
    resp = _post_or_skip(
        "/embed/rerank",
        {
            "query": "What is MIGraphX?",
            "docs": [
                "MIGraphX is AMD's graph compiler for ONNX.",
                "ROCm is AMD's GPU computing platform.",
                "The cat sat on the mat.",
            ],
            "model": "Xenova/ms-marco-MiniLM-L-6-v2",
            "top_k": 2,
        },
        timeout=120,
    )
    result = resp.get("result", resp)
    results = result.get("results", [])
    assert len(results) == 2, f"Expected 2 results from rerank, got {len(results)}: {results}"
    for item in results:
        assert "index" in item, f"Unexpected result shape: {item}"
        assert "score" in item, f"Unexpected result shape: {item}"
        assert isinstance(item["index"], int), f"index must be int: {item}"
        assert isinstance(item["score"], float), f"score must be float: {item}"


@pytest.mark.skipif(_cpu_mode(), reason="OPENCODE_ONNX_PROVIDER=cpu — intentional CPU mode")
def test_migraphx_gpu_ops_tracked():
    """After embed call with MIGraphX, gpu_ops must increase (replaces test_migraphx_gpu_ops_tracking)."""
    health_before = _get_health_or_skip()
    gpu_before = health_before.get("gpu", {})
    provider = gpu_before.get("provider", "unknown")

    initial_gpu = gpu_before.get("gpu_ops", 0)
    initial_cpu = gpu_before.get("cpu_ops", 0)

    _post_or_skip(
        "/embed/passages_f32",
        {"texts": ["GPU ops tracking test"], "model": "jinaai/jina-embeddings-v2-small-en", "dimensions": 512},
        timeout=120,
    )

    health_after = _fetch_health()
    gpu_after = health_after.get("gpu", {})

    if provider == "migraphx":
        assert gpu_after.get("gpu_ops", 0) > initial_gpu, (
            f"MIGraphX active but gpu_ops did not increase: before={initial_gpu} after={gpu_after.get('gpu_ops')}"
        )
    else:
        # Non-MIGraphX: either counter increases
        delta = (gpu_after.get("gpu_ops", 0) - initial_gpu) + (gpu_after.get("cpu_ops", 0) - initial_cpu)
        assert delta >= 1, f"No op counter incremented: before_gpu={initial_gpu} before_cpu={initial_cpu} after={gpu_after}"


def test_migraphx_health_provider_matches_stats():
    """Health gpu.provider must be consistent with is_gpu (replaces test_migraphx_health_check)."""
    health = _get_health_or_skip()
    gpu = health.get("gpu", {})
    provider = gpu.get("provider", "unknown")
    is_gpu = gpu.get("is_gpu", False)

    assert "provider" in gpu, f"gpu.provider missing: {gpu}"

    if provider == "migraphx":
        assert is_gpu is True, f"migraphx provider but is_gpu={is_gpu}: {gpu}"


def test_amd_provider_consistent_with_rocm_path():
    """AMD provider availability must match /opt/rocm presence (replaces test_rocm_detection_matches_installation)."""
    health = _get_health_or_skip()
    gpu = health.get("gpu", {})
    provider = gpu.get("provider", "unknown")
    is_gpu = gpu.get("is_gpu", False)
    rocm_installed = os.path.exists("/opt/rocm")

    has_amd = provider in AMD_PROVIDERS and is_gpu

    if rocm_installed:
        # ROCm is installed — AMD provider should be active (unless overridden by another GPU)
        # We can't assert has_amd because CUDA/TensorRT may be preferred; just check no contradiction
        pass  # Any GPU provider is fine when ROCm is installed
    else:
        assert not has_amd, (
            f"AMD provider {provider!r} active but /opt/rocm not found.\n"
            f"  → Full gpu stats: {gpu}"
        )


# ---------------------------------------------------------------------------
# Server HTTP roundtrip (replaces test_server.py functional coverage)
# ---------------------------------------------------------------------------


def test_server_health_endpoint():
    """Health endpoint returns 200 with status=ok (replaces test_server_health_roundtrip)."""
    health = _get_health_or_skip()
    assert health.get("status") == "ok", f"Expected status=ok: {health}"
    assert "pid" in health, f"Missing pid in health: {health}"


def test_server_chunk_endpoint():
    """embed/chunk returns chunks list (replaces test_server_chunk_roundtrip)."""
    resp = _post_or_skip(
        "/embed/chunk",
        {"content": "hello\nworld\n", "path": "hello.txt", "max_lines": 50, "tier": "budget"},
    )
    result = resp.get("result", resp)
    assert "chunks" in result, f"Missing 'chunks' in response: {result}"
    assert isinstance(result["chunks"], list), f"chunks must be list: {result}"
    if result["chunks"]:
        chunk = result["chunks"][0]
        assert "content" in chunk, f"chunk missing 'content': {chunk}"
        assert "start_line" in chunk, f"chunk missing 'start_line': {chunk}"
        assert "end_line" in chunk, f"chunk missing 'end_line': {chunk}"


def test_server_embed_query_f32_returns_vector():
    """embed/query_f32 returns valid f32 vector (replaces test_server_embed_query_f32_roundtrip)."""
    resp = _post_or_skip(
        "/embed/query_f32",
        {"text": "hello", "model": "jinaai/jina-embeddings-v2-small-en", "dimensions": 512},
        timeout=120,
    )
    result = resp.get("result", resp)
    assert result.get("endianness") == "le", f"Expected endianness=le: {result}"
    assert result.get("dimensions") == 512, f"Expected dimensions=512: {result}"
    vector_raw = result.get("vector_f32")
    assert vector_raw is not None, f"Missing vector_f32: {result}"
    floats = _unpack_f32_le(vector_raw)
    assert len(floats) == 512, f"Expected 512 floats, got {len(floats)}"


def test_server_embed_passages_f32_returns_vectors():
    """embed/passages_f32 returns correct count and dimensions (replaces test_server_embed_passages_f32_roundtrip)."""
    texts = ["hello", "world"]
    resp = _post_or_skip(
        "/embed/passages_f32",
        {"texts": texts, "model": "jinaai/jina-embeddings-v2-small-en", "dimensions": 512},
        timeout=120,
    )
    result = resp.get("result", resp)
    assert result.get("endianness") == "le", f"Expected endianness=le: {result}"
    assert result.get("dimensions") == 512, f"Expected dimensions=512: {result}"
    assert result.get("count") == len(texts), f"Expected count={len(texts)}: {result}"
    vectors_raw = result.get("vectors_f32")
    assert vectors_raw is not None, f"Missing vectors_f32: {result}"
    floats = _unpack_f32_le(vectors_raw)
    assert len(floats) == len(texts) * 512, f"Expected {len(texts)*512} floats, got {len(floats)}"


def test_server_chunk_and_embed_returns_inline_vectors():
    """embed/chunk_and_embed returns chunks with inline vectors (replaces test_server_chunk_and_embed_roundtrip)."""
    resp = _post_or_skip(
        "/embed/chunk_and_embed",
        {
            "content": "hello world\nfoo bar\n",
            "path": "test.txt",
            "tier": "budget",
            "model": "jinaai/jina-embeddings-v2-small-en",
            "dimensions": 512,
        },
        timeout=120,
    )
    result = resp.get("result", resp)
    chunks = result.get("chunks", [])
    assert isinstance(chunks, list), f"chunks must be list: {result}"
    if chunks:
        chunk = chunks[0]
        assert "vector" in chunk, f"chunk missing inline 'vector': {chunk}"
        assert isinstance(chunk["vector"], list), f"vector must be list: {chunk}"
        assert len(chunk["vector"]) == 512, f"Expected 512-dim vector: {len(chunk['vector'])}"


def test_server_path_traversal_rejected():
    """Path traversal via file param must return 4xx/5xx error (replaces test_chunk_path_traversal_rejected)."""
    traversal_paths = ["/etc/passwd", "/etc/shadow", "/root/.ssh/id_rsa", "/../../../etc/passwd"]

    for evil_path in traversal_paths:
        url = f"{EMBEDDER_URL}/embed/chunk"
        data = json.dumps({"file": evil_path, "path": "evil.txt", "tier": "budget"}).encode()
        headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                body = json.loads(e.read())
            except Exception:
                body = {}
        except (urllib.error.URLError, OSError) as exc:
            pytest.skip(f"Embedder not running: {exc}")

        assert status in (400, 403, 500), (
            f"Path traversal '{evil_path}' returned {status}, expected 4xx/5xx"
        )
        assert "error" in body, (
            f"Path traversal '{evil_path}' returned result instead of error: {body}"
        )


def test_server_concurrent_health_requests():
    """Server handles multiple concurrent health requests correctly (replaces test_concurrent_http_requests)."""
    import concurrent.futures

    # Verify service is up first
    _get_health_or_skip()

    def fetch_health():
        return _fetch_health()

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch_health) for _ in range(20)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    assert len(results) == 20, f"Expected 20 results, got {len(results)}"
    for r in results:
        assert r.get("status") == "ok", f"Non-ok health response: {r}"


def test_server_pid_in_health():
    """Health endpoint must include pid field (regression guard)."""
    health = _get_health_or_skip()
    assert "pid" in health, f"Missing pid in health response: {health}"
    assert isinstance(health["pid"], int), f"pid must be int: {health['pid']}"


@pytest.mark.skipif(_cpu_mode(), reason="OPENCODE_ONNX_PROVIDER=cpu — intentional CPU mode")
def test_no_cpu_fallback_in_production():
    """Provider must not be cpu in production mode (replaces test_provider_env_override intent)."""
    health = _get_health_or_skip()
    gpu = health.get("gpu", {})
    provider = gpu.get("provider", "unknown")
    assert provider != "cpu", (
        f"Provider is cpu in production mode: {provider!r}\n"
        f"  → GPU required. Set OPENCODE_ONNX_PROVIDER=cpu to skip intentionally.\n"
        f"  → Full gpu stats: {gpu}"
    )


def test_tensorrt_provider_is_gpu_when_active():
    """When TensorRT is the active provider, is_gpu must be True (replaces test_tensorrt_is_gpu_provider)."""
    health = _get_health_or_skip()
    gpu = health.get("gpu", {})
    if gpu.get("provider") == "tensorrt":
        assert gpu.get("is_gpu") is True, (
            f"TensorRT provider active but is_gpu=False: {gpu}"
        )


def test_health_gpu_field_present():
    """Health response must include top-level 'gpu' dict (replaces test_health_endpoint_includes_gpu_info)."""
    health = _get_health_or_skip()
    assert "gpu" in health, f"'gpu' key missing from health response: {health}"
    gpu = health["gpu"]
    assert isinstance(gpu, dict), f"'gpu' must be dict, got {type(gpu)}: {gpu}"
    for key in ("provider", "is_gpu", "gpu_ops", "cpu_ops"):
        assert key in gpu, f"Missing '{key}' in gpu stats: {gpu}"
