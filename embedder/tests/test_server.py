import asyncio
import base64
import os
import socket
import struct
import tempfile
from pathlib import Path

import aiohttp
import pytest

from opencode_embedder.server import ModelServer, _kill_process_group, _setup_process_group


def free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def unpack_f32_le(data: bytes | str) -> list[float]:
    if isinstance(data, str):
        data = base64.b64decode(data)
    if not data:
        return []
    count = len(data) // 4
    return list(struct.unpack("<" + "f" * count, data))


@pytest.fixture(autouse=True)
def _disable_warmup(monkeypatch: pytest.MonkeyPatch):
    async def noop(self: ModelServer):
        return None

    monkeypatch.setattr(ModelServer, "_warmup_models", noop)


async def _wait_ready(url: str, tries: int = 200) -> bool:
    for _ in range(tries):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=1)) as r:
                    if r.status == 200:
                        return True
        except Exception:
            await asyncio.sleep(0.1)
    return False


@pytest.mark.asyncio
async def test_server_health_roundtrip(monkeypatch: pytest.MonkeyPatch):
    port = free_port()
    monkeypatch.setenv("OPENCODE_EMBED_HTTP_PORT", str(port))

    srv = ModelServer(embed_workers=1)
    task = asyncio.create_task(srv.serve())

    base = f"http://127.0.0.1:{port}"
    assert await _wait_ready(f"{base}/health"), "Server should start within 20 seconds"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/health") as resp:
                assert resp.status == 200
                data = await resp.json()
        assert data["result"]["status"] == "ok"
        assert data["result"]["pid"] == os.getpid()
    finally:
        srv._shutdown.set()
        await task


@pytest.mark.asyncio
async def test_server_chunk_roundtrip(monkeypatch: pytest.MonkeyPatch):
    port = free_port()
    monkeypatch.setenv("OPENCODE_EMBED_HTTP_PORT", str(port))

    srv = ModelServer(embed_workers=1)
    task = asyncio.create_task(srv.serve())

    base = f"http://127.0.0.1:{port}"
    assert await _wait_ready(f"{base}/health"), "Server should start within 20 seconds"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base}/embed/chunk",
                json={
                    "content": "hello\nworld\n",
                    "path": "hello.txt",
                    "max_lines": 50,
                    "tier": "budget",
                },
            ) as resp:
                assert resp.status == 200
                data = await resp.json()
        assert "result" in data
        assert "chunks" in data["result"]
        assert isinstance(data["result"]["chunks"], list)
        if data["result"]["chunks"]:
            c = data["result"]["chunks"][0]
            assert "content" in c
            assert "start_line" in c
            assert "end_line" in c
            assert "vector" not in c
    finally:
        srv._shutdown.set()
        await task


@pytest.mark.asyncio
async def test_server_chunk_file_roundtrip(monkeypatch: pytest.MonkeyPatch):
    port = free_port()
    monkeypatch.setenv("OPENCODE_EMBED_HTTP_PORT", str(port))

    with tempfile.TemporaryDirectory() as tmp:
        file = Path(tmp) / "hello.txt"
        file.write_text("hello\nworld\n", encoding="utf-8")

        # CWD must include the file's directory so path validation passes
        original_cwd = os.getcwd()
        os.chdir(tmp)

        srv = ModelServer(embed_workers=1)
        task = asyncio.create_task(srv.serve())

        base = f"http://127.0.0.1:{port}"
        assert await _wait_ready(f"{base}/health"), "Server should start within 20 seconds"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{base}/embed/chunk_file",
                    json={"file": str(file), "path": "hello.txt", "tier": "budget"},
                ) as resp:
                    assert resp.status == 200
                    data = await resp.json()
            assert "result" in data
            assert "chunks" in data["result"]
            assert isinstance(data["result"]["chunks"], list)
        finally:
            os.chdir(original_cwd)
            srv._shutdown.set()
            await task


@pytest.mark.asyncio
async def test_server_embed_query_f32_roundtrip(monkeypatch: pytest.MonkeyPatch):
    port = free_port()
    monkeypatch.setenv("OPENCODE_EMBED_HTTP_PORT", str(port))

    import opencode_embedder.server as server

    def fake_embed_query_f32_bytes(text: str, *, model: str, dimensions: int):
        floats = [0.0, 1.0, 2.0, 3.0]
        return struct.pack("<ffff", *floats), len(floats)

    monkeypatch.setattr(server, "embed_query_f32_bytes", fake_embed_query_f32_bytes)

    srv = ModelServer(embed_workers=1)
    task = asyncio.create_task(srv.serve())

    base = f"http://127.0.0.1:{port}"
    assert await _wait_ready(f"{base}/health"), "Server should start within 20 seconds"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base}/embed/query_f32",
                json={"text": "hello", "model": "test", "dimensions": 4},
            ) as resp:
                assert resp.status == 200
                data = await resp.json()
        result = data["result"]
        assert result["endianness"] == "le"
        assert result["dimensions"] == 4
        # HTTP JSON transport base64-encodes binary fields via _jsonify()
        assert isinstance(result["vector_f32"], str)
        assert unpack_f32_le(result["vector_f32"]) == [0.0, 1.0, 2.0, 3.0]
    finally:
        srv._shutdown.set()
        await task


@pytest.mark.asyncio
async def test_server_embed_passages_f32_roundtrip(monkeypatch: pytest.MonkeyPatch):
    port = free_port()
    monkeypatch.setenv("OPENCODE_EMBED_HTTP_PORT", str(port))

    import opencode_embedder.server as server

    def fake_embed_passages_f32_bytes(texts: list[str], *, model: str, dimensions: int):
        floats = [float(i * 10 + j) for i in range(len(texts)) for j in range(dimensions)]
        return struct.pack("<" + "f" * len(floats), *floats), dimensions, len(texts)

    monkeypatch.setattr(server, "embed_passages_f32_bytes", fake_embed_passages_f32_bytes)

    srv = ModelServer(embed_workers=1)
    task = asyncio.create_task(srv.serve())

    base = f"http://127.0.0.1:{port}"
    assert await _wait_ready(f"{base}/health"), "Server should start within 20 seconds"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base}/embed/passages_f32",
                json={"texts": ["a", "b"], "model": "test", "dimensions": 4},
            ) as resp:
                assert resp.status == 200
                data = await resp.json()
        result = data["result"]
        assert result["endianness"] == "le"
        assert result["dimensions"] == 4
        assert result["count"] == 2
        # vectors_f32 is base64-encoded in HTTP/JSON response
        assert unpack_f32_le(result["vectors_f32"]) == [0.0, 1.0, 2.0, 3.0, 10.0, 11.0, 12.0, 13.0]
    finally:
        srv._shutdown.set()
        await task


@pytest.mark.asyncio
async def test_server_chunk_and_embed_roundtrip(monkeypatch: pytest.MonkeyPatch):
    port = free_port()
    monkeypatch.setenv("OPENCODE_EMBED_HTTP_PORT", str(port))

    import opencode_embedder.server as server

    def fake_chunk(self: ModelServer, params: dict) -> list[dict]:
        return [
            {
                "content": "a",
                "start_line": 1,
                "end_line": 1,
                "chunk_type": "block",
                "language": "txt",
            },
            {
                "content": "b",
                "start_line": 2,
                "end_line": 2,
                "chunk_type": "block",
                "language": "txt",
            },
        ]

    def fake_embed_passages(texts: list[str], *, model: str, dimensions: int):
        return [[float(i * 10 + j) for j in range(4)] for i in range(len(texts))]

    monkeypatch.setattr(ModelServer, "_chunk", fake_chunk)
    monkeypatch.setattr(server, "embed_passages", fake_embed_passages)

    srv = ModelServer(embed_workers=1)
    task = asyncio.create_task(srv.serve())

    base = f"http://127.0.0.1:{port}"
    assert await _wait_ready(f"{base}/health"), "Server should start within 20 seconds"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base}/embed/chunk_and_embed",
                json={
                    "content": "x",
                    "path": "x.txt",
                    "tier": "budget",
                    "model": "test",
                    "dimensions": 4,
                },
            ) as resp:
                assert resp.status == 200
                data = await resp.json()
        result = data["result"]
        assert len(result["chunks"]) == 2
        # chunk_and_embed returns vectors inline in each chunk (not as separate vectors_f32)
        assert result["chunks"][0]["vector"] == [0.0, 1.0, 2.0, 3.0]
        assert result["chunks"][1]["vector"] == [10.0, 11.0, 12.0, 13.0]
    finally:
        srv._shutdown.set()
        await task


@pytest.mark.asyncio
async def test_server_chunk_and_embed_file_roundtrip(monkeypatch: pytest.MonkeyPatch):
    port = free_port()
    monkeypatch.setenv("OPENCODE_EMBED_HTTP_PORT", str(port))

    with tempfile.TemporaryDirectory() as tmp:
        file = Path(tmp) / "hello.txt"
        file.write_text("hello\nworld\n", encoding="utf-8")

        import opencode_embedder.server as server

        def fake_chunk(self: ModelServer, params: dict) -> list[dict]:
            return [
                {
                    "content": "c",
                    "start_line": 1,
                    "end_line": 1,
                    "chunk_type": "block",
                    "language": "txt",
                }
            ]

        def fake_embed_passages(texts: list[str], *, model: str, dimensions: int):
            return [[100.0, 101.0, 102.0, 103.0]]

        monkeypatch.setattr(ModelServer, "_chunk", fake_chunk)
        monkeypatch.setattr(server, "embed_passages", fake_embed_passages)

        srv = ModelServer(embed_workers=1)
        task = asyncio.create_task(srv.serve())

        base = f"http://127.0.0.1:{port}"
        assert await _wait_ready(f"{base}/health"), "Server should start within 20 seconds"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{base}/embed/chunk_and_embed",
                    json={
                        "file": str(file),
                        "path": "hello.txt",
                        "tier": "budget",
                        "model": "test",
                        "dimensions": 4,
                    },
                ) as resp:
                    assert resp.status == 200
                    data = await resp.json()
            result = data["result"]
            assert len(result["chunks"]) == 1
            assert result["chunks"][0]["vector"] == [100.0, 101.0, 102.0, 103.0]
        finally:
            srv._shutdown.set()
            await task


# ---------------------------------------------------------------------------
# Process group tests
# ---------------------------------------------------------------------------


def test_setup_process_group_does_not_raise():
    """Verify _setup_process_group() runs without error."""
    _setup_process_group()


def test_kill_process_group_function_exists():
    """Verify _kill_process_group function is importable and callable."""
    assert callable(_kill_process_group), "_kill_process_group should be callable"


# ---------------------------------------------------------------------------
# Thread safety tests
# ---------------------------------------------------------------------------


def test_provider_detection_thread_safety():
    """Verify provider detection is thread-safe."""
    import concurrent.futures

    import opencode_embedder.embeddings as embeddings
    from opencode_embedder.embeddings import _get_onnx_providers

    embeddings._detected_providers = None
    embeddings._provider_detection_done = False

    results = []
    errors = []

    def detect():
        try:
            results.append(_get_onnx_providers())
        except Exception as e:
            errors.append(e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(detect) for _ in range(10)]
        concurrent.futures.wait(futures)

    assert len(errors) == 0, f"Thread safety errors: {errors}"
    assert len(results) == 10
    first = results[0]
    for r in results[1:]:
        assert r == first, "All threads should get same cached result"


def test_provider_detection_caches_result():
    """Verify provider detection result is cached."""
    import opencode_embedder.embeddings as embeddings
    from opencode_embedder.embeddings import _get_onnx_providers

    embeddings._detected_providers = None
    embeddings._provider_detection_done = False

    result1 = _get_onnx_providers()
    result2 = _get_onnx_providers()

    assert result1 == result2
    assert embeddings._provider_detection_done is True


# ---------------------------------------------------------------------------
# HTTP concurrency test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_http_requests(monkeypatch: pytest.MonkeyPatch):
    """Test that the server handles concurrent HTTP requests correctly."""
    port = free_port()
    monkeypatch.setenv("OPENCODE_EMBED_HTTP_PORT", str(port))

    srv = ModelServer(embed_workers=4)
    task = asyncio.create_task(srv.serve())

    base = f"http://127.0.0.1:{port}"
    assert await _wait_ready(f"{base}/health"), "Server should start within 20 seconds"

    try:

        async def fetch(session: aiohttp.ClientSession) -> dict:
            async with session.get(f"{base}/health") as resp:
                assert resp.status == 200
                return await resp.json()

        async with aiohttp.ClientSession() as session:
            results = await asyncio.gather(*[fetch(session) for _ in range(20)])

        assert len(results) == 20
        for r in results:
            assert r["result"]["status"] == "ok"
    finally:
        srv._shutdown.set()
        await task


# ---------------------------------------------------------------------------
# Path traversal rejection tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chunk_path_traversal_rejected(monkeypatch: pytest.MonkeyPatch):
    """Path traversal via 'file' param must be rejected, not silently succeed."""
    port = free_port()
    monkeypatch.setenv("OPENCODE_EMBED_HTTP_PORT", str(port))

    srv = ModelServer(embed_workers=1)
    task = asyncio.create_task(srv.serve())

    base = f"http://127.0.0.1:{port}"
    assert await _wait_ready(f"{base}/health"), "Server should start within 20 seconds"

    traversal_paths = [
        "/etc/passwd",
        "/etc/shadow",
        "/root/.ssh/id_rsa",
        "/../../../etc/passwd",
    ]

    try:
        async with aiohttp.ClientSession() as session:
            for evil_path in traversal_paths:
                async with session.post(
                    f"{base}/embed/chunk",
                    json={"file": evil_path, "path": "evil.txt", "tier": "budget"},
                ) as resp:
                    # Must NOT return 200 with empty content u2014 must be an error status
                    assert resp.status in (400, 403, 500), (
                        f"Path traversal '{evil_path}' returned {resp.status}, "
                        f"expected 4xx/5xx error"
                    )
                    data = await resp.json()
                    # Must contain error key, not a result with empty chunks
                    assert "error" in data, (
                        f"Path traversal '{evil_path}' returned result instead of error: {data}"
                    )
    finally:
        srv._shutdown.set()
        await task


@pytest.mark.asyncio
async def test_chunk_path_traversal_prefix_attack_rejected(monkeypatch: pytest.MonkeyPatch):
    """Prefix-adjacent paths (e.g. ~/.opencode_evil) must not match ~/.opencode."""
    port = free_port()
    monkeypatch.setenv("OPENCODE_EMBED_HTTP_PORT", str(port))

    srv = ModelServer(embed_workers=1)
    task = asyncio.create_task(srv.serve())

    base = f"http://127.0.0.1:{port}"
    assert await _wait_ready(f"{base}/health"), "Server should start within 20 seconds"

    import tempfile as _tempfile
    with _tempfile.TemporaryDirectory() as tmp:
        evil_file = Path(tmp) / "secret.txt"
        evil_file.write_text("secret")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{base}/embed/chunk",
                    json={"file": str(evil_file), "path": "evil.txt", "tier": "budget"},
                ) as resp:
                    assert resp.status in (400, 403, 500), (
                        f"Path outside allowed dirs returned {resp.status}, expected error"
                    )
                    data = await resp.json()
                    assert "error" in data
        finally:
            srv._shutdown.set()
            await task


@pytest.mark.asyncio
async def test_chunk_file_within_cwd_allowed(monkeypatch: pytest.MonkeyPatch):
    """Files within CWD must still be readable (regression guard)."""
    port = free_port()
    monkeypatch.setenv("OPENCODE_EMBED_HTTP_PORT", str(port))

    import tempfile as _tempfile
    with _tempfile.TemporaryDirectory() as tmp:
        original_cwd = os.getcwd()
        os.chdir(tmp)

        file = Path(tmp) / "hello.txt"
        file.write_text("hello world", encoding="utf-8")

        srv = ModelServer(embed_workers=1)
        task = asyncio.create_task(srv.serve())

        base = f"http://127.0.0.1:{port}"
        assert await _wait_ready(f"{base}/health"), "Server should start within 20 seconds"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{base}/embed/chunk",
                    json={"file": str(file), "path": "hello.txt", "tier": "budget"},
                ) as resp:
                    assert resp.status == 200, (
                        f"File within CWD should be readable, got {resp.status}"
                    )
                    data = await resp.json()
                    assert "result" in data
                    assert "chunks" in data["result"]
        finally:
            os.chdir(original_cwd)
            srv._shutdown.set()
            await task
