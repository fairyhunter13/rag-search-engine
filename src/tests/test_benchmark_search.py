"""Benchmark regression test: search_code p95 latency gate.

Indexes a small synthetic Python corpus and runs 20 search calls.
Asserts p95 latency < 500 ms.  Skips if GPU is unavailable.

Run with:
  pytest src/tests/test_benchmark_search.py -v -m perf
"""
from __future__ import annotations

import asyncio
import statistics
import time

import pytest

pytest.importorskip("lancedb")
pytest.importorskip("pyarrow")
pytest.importorskip("mcp")

from opencode_search import config
from opencode_search.mcp import (
    _release_stale_project_watches,
    build,
    overview,
    search,
)


async def index_project(path, watch=False, force=False, follow_symlinks=True):
    return await build(project_path=path, action="index", watch=watch, force=force)
async def search_code(query, project_paths=None, top_k=10, use_rerank=True):
    return await search(query=query, project_paths=project_paths, top_k=top_k)
async def project_status(path): return await overview(project_path=path, what="status")
from opencode_search.search import clear_search_cache  # noqa: E402
from opencode_search.watcher import watcher_manager  # noqa: E402

pytestmark = [pytest.mark.perf, pytest.mark.integration, pytest.mark.runtime_deps, pytest.mark.gpu]

_P95_LIMIT_MS = 500.0
_WARM_UP_CALLS = 3
_MEASURE_CALLS = 20

_CORPUS: list[tuple[str, str]] = [
    ("auth.py", 'def authenticate(token: str) -> bool:\n    """Verify JWT token."""\n    return token.startswith("Bearer ")\n'),
    ("database.py", 'def get_connection(url: str):\n    """Return a database connection."""\n    import sqlite3\n    return sqlite3.connect(url)\n'),
    ("cache.py", 'class LRUCache:\n    """Least-recently-used cache with TTL."""\n    def __init__(self, maxsize: int = 128) -> None:\n        self._data: dict = {}\n        self.maxsize = maxsize\n'),
    ("api.py", 'def list_users(limit: int = 10) -> list[dict]:\n    """Return paginated user list."""\n    return []\n\ndef create_user(name: str, email: str) -> dict:\n    return {"name": name, "email": email}\n'),
    ("models.py", 'from dataclasses import dataclass\n\n@dataclass\nclass User:\n    id: int\n    name: str\n    email: str\n    active: bool = True\n'),
    ("utils.py", 'import hashlib\n\ndef hash_password(password: str) -> str:\n    """SHA-256 hash for password storage."""\n    return hashlib.sha256(password.encode()).hexdigest()\n'),
    ("config.py", 'import os\n\nDATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///app.db")\nSECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret")\nDEBUG = os.environ.get("DEBUG", "0") == "1"\n'),
    ("search_engine.py", 'def search_documents(query: str, docs: list[str]) -> list[str]:\n    """Simple keyword search over documents."""\n    q = query.lower()\n    return [d for d in docs if q in d.lower()]\n'),
    ("pagination.py", 'def paginate(items: list, page: int, per_page: int = 20) -> dict:\n    """Paginate a list of items."""\n    start = (page - 1) * per_page\n    return {"items": items[start:start + per_page], "page": page, "total": len(items)}\n'),
    ("middleware.py", 'def rate_limit(max_requests: int = 100):\n    """Decorator to rate-limit endpoint calls."""\n    def decorator(func):\n        def wrapper(*args, **kwargs):\n            return func(*args, **kwargs)\n        return wrapper\n    return decorator\n'),
]

_QUERIES = [
    "authenticate JWT token",
    "database connection sqlite",
    "LRU cache implementation",
    "list users pagination",
    "user dataclass model",
    "hash password SHA256",
    "environment configuration",
    "keyword document search",
    "paginate items page",
    "rate limit decorator",
]


async def _wait_indexed(project_root: str, timeout_s: float = 60.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        status = await project_status(path=project_root)
        if status.get("indexed") and not status.get("indexing_running"):
            return
        await asyncio.sleep(0.3)
    raise TimeoutError(f"Project not indexed within {timeout_s}s")


@pytest.mark.asyncio
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
        result = await index_project(path=str(project_root), watch=False)
        assert result.get("status") == "indexing", f"unexpected: {result}"
        await _wait_indexed(str(project_root))

        status = await project_status(path=str(project_root))
        assert status.get("indexed"), f"project not indexed: {status}"
        assert (status.get("chunks") or 0) > 0, "no chunks stored"

        clear_search_cache()

        # Warm up: let ONNX initialise its CUDA graph and load the model.
        for q in _QUERIES[:_WARM_UP_CALLS]:
            await search_code(query=q, project_paths=[str(project_root)], top_k=5)
        clear_search_cache()

        # Measure.
        latencies: list[float] = []
        queries = (_QUERIES * ((_MEASURE_CALLS // len(_QUERIES)) + 1))[:_MEASURE_CALLS]
        for q in queries:
            t0 = time.perf_counter()
            res = await search_code(query=q, project_paths=[str(project_root)], top_k=5)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            assert "error" not in res, f"search error: {res}"
            latencies.append(elapsed_ms)
            clear_search_cache()

        latencies.sort()
        p50 = statistics.median(latencies)
        p95 = latencies[int(len(latencies) * 0.95)]
        p99 = latencies[-1]

        print(f"\n  Latency over {_MEASURE_CALLS} calls: p50={p50:.1f}ms  p95={p95:.1f}ms  p99={p99:.1f}ms")

        assert p95 < _P95_LIMIT_MS, (
            f"p95 latency {p95:.1f}ms exceeds {_P95_LIMIT_MS}ms gate "
            f"(p50={p50:.1f}ms, p99={p99:.1f}ms)"
        )
    finally:
        await watcher_manager.stop_all()
        await _release_stale_project_watches()
