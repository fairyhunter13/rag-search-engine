"""Benchmark and GPU smoke tests: p95 search latency gate and real GPU embeddings."""
from __future__ import annotations

import asyncio
import math
import statistics
import time

import pytest

pytest.importorskip("lancedb")
pytest.importorskip("pyarrow")
pytest.importorskip("mcp")

from opencode_search import config
from opencode_search.mcp import _release_stale_project_watches, build, overview, search
from opencode_search.search import clear_search_cache
from opencode_search.watcher import watcher_manager


async def _index_project(path, watch=False, force=False):
    return await build(project_path=path, action="index", watch=watch, force=force)

async def _search_code(query, project_paths=None, top_k=10, use_rerank=True):
    return await search(query=query, project_paths=project_paths, top_k=top_k)

async def _project_status(path):
    return await overview(project_path=path, what="status")


# === Benchmark: p95 search latency ===

pytestmark_bench = [pytest.mark.perf, pytest.mark.integration, pytest.mark.runtime_deps, pytest.mark.gpu]

_P95_LIMIT_MS = 500.0
_WARM_UP_CALLS = 3
_MEASURE_CALLS = 20

_CORPUS: list[tuple[str, str]] = [
    ("auth.py", 'def authenticate(token: str) -> bool:\n    """Verify JWT token."""\n    return token.startswith("Bearer ")\n'),
    ("database.py", 'def get_connection(url: str):\n    """Return a database connection."""\n    import sqlite3\n    return sqlite3.connect(url)\n'),
    ("cache.py", 'class LRUCache:\n    """Least-recently-used cache with TTL."""\n    def __init__(self, maxsize: int = 128) -> None:\n        self._data: dict = {}\n'),
    ("api.py", 'def list_users(limit: int = 10) -> list[dict]:\n    """Return paginated user list."""\n    return []\n'),
    ("models.py", 'from dataclasses import dataclass\n\n@dataclass\nclass User:\n    id: int\n    name: str\n    email: str\n'),
    ("utils.py", 'import hashlib\n\ndef hash_password(password: str) -> str:\n    return hashlib.sha256(password.encode()).hexdigest()\n'),
    ("config.py", 'import os\n\nDATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///app.db")\n'),
    ("search_engine.py", 'def search_documents(query: str, docs: list[str]) -> list[str]:\n    q = query.lower()\n    return [d for d in docs if q in d.lower()]\n'),
    ("pagination.py", 'def paginate(items: list, page: int, per_page: int = 20) -> dict:\n    start = (page - 1) * per_page\n    return {"items": items[start:start + per_page], "page": page}\n'),
    ("middleware.py", 'def rate_limit(max_requests: int = 100):\n    def decorator(func):\n        def wrapper(*args, **kwargs):\n            return func(*args, **kwargs)\n        return wrapper\n    return decorator\n'),
]

_QUERIES = [
    "authenticate JWT token", "database connection sqlite", "LRU cache implementation",
    "list users pagination", "user dataclass model", "hash password SHA256",
    "environment configuration", "keyword document search", "paginate items page",
    "rate limit decorator",
]


async def _wait_indexed(project_root: str, timeout_s: float = 60.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        status = await _project_status(path=project_root)
        if status.get("indexed") and not status.get("indexing_running"):
            return
        await asyncio.sleep(0.3)
    raise TimeoutError(f"Project not indexed within {timeout_s}s")


@pytest.mark.asyncio
@pytest.mark.perf
@pytest.mark.integration
@pytest.mark.runtime_deps
@pytest.mark.gpu
async def test_search_code_p95_latency(tmp_path, monkeypatch):
    """p95 search latency must stay below 500 ms on a 10-file synthetic corpus."""
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(config, "REGISTRY_PATH", registry_path)

    project_root = tmp_path / "corpus"
    project_root.mkdir()
    for filename, content in _CORPUS:
        (project_root / filename).write_text(content, encoding="utf-8")

    await watcher_manager.stop_all()
    try:
        result = await _index_project(path=str(project_root), watch=False)
        assert result.get("status") == "indexing", f"unexpected: {result}"
        await _wait_indexed(str(project_root))

        status = await _project_status(path=str(project_root))
        assert status.get("indexed") and (status.get("chunks") or 0) > 0

        clear_search_cache()
        for q in _QUERIES[:_WARM_UP_CALLS]:
            await _search_code(query=q, project_paths=[str(project_root)], top_k=5)
        clear_search_cache()

        latencies: list[float] = []
        queries = (_QUERIES * ((_MEASURE_CALLS // len(_QUERIES)) + 1))[:_MEASURE_CALLS]
        for q in queries:
            t0 = time.perf_counter()
            res = await _search_code(query=q, project_paths=[str(project_root)], top_k=5)
            latencies.append((time.perf_counter() - t0) * 1000)
            assert "error" not in res
            clear_search_cache()

        latencies.sort()
        p50 = statistics.median(latencies)
        p95 = latencies[int(len(latencies) * 0.95)]
        p99 = latencies[-1]
        print(f"\n  p50={p50:.1f}ms  p95={p95:.1f}ms  p99={p99:.1f}ms")
        assert p95 < _P95_LIMIT_MS, f"p95 {p95:.1f}ms exceeds {_P95_LIMIT_MS}ms"
    finally:
        await watcher_manager.stop_all()
        await _release_stale_project_watches()


# === GPU smoke test ===

@pytest.mark.integration
@pytest.mark.runtime_deps
@pytest.mark.gpu
def test_real_gpu_embedding_and_rerank_smoke():
    pytest.importorskip("fastembed")
    pytest.importorskip("onnxruntime")

    from opencode_search.config import DEFAULT_DIMS, DEFAULT_EMBED_MODEL, DEFAULT_RERANK_MODEL
    from opencode_search.embeddings import assert_gpu_available, embed_passages, embed_query, rerank

    assert_gpu_available()

    vector = embed_query("authorization bearer token refresh flow", model=DEFAULT_EMBED_MODEL, dimensions=DEFAULT_DIMS)
    assert len(vector) == DEFAULT_DIMS
    assert all(math.isfinite(v) for v in vector)
    assert any(v != 0.0 for v in vector)

    passages = embed_passages(
        ["middleware refreshes expired bearer token", "grid layout for settings screen"],
        model=DEFAULT_EMBED_MODEL, dimensions=DEFAULT_DIMS,
    )
    assert len(passages) == 2

    ranked = rerank(
        "token refresh middleware",
        ["css grid layout", "middleware to refresh expired access token", "postgres vacuum"],
        model=DEFAULT_RERANK_MODEL, top_k=2,
    )
    assert ranked and ranked[0][0] == 1
