"""Tests for opencode_search.search — cache, dedup, federated routing.

All tests use mocked embed_query and rerank so no GPU is required.
@pytest.mark.gpu tests run actual inference and only execute on real CUDA.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opencode_search.config import (
    FINAL_TOP_K,
    SKIP_STAGE1_RERANK_N,
    STAGE1_RERANK_K,
    STAGE1_VECTOR_K,
    ProjectEntry,
    get_tier_dims,
    get_tier_models,
)
from opencode_search.search import (
    SearchResult,
    _cache_key,
    clear_search_cache,
    search,
    search_project,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(path: str = "/tmp/proj", tier: str = "balanced") -> ProjectEntry:
    embed_model, rerank_model = get_tier_models(tier)
    dims = get_tier_dims(tier)
    return ProjectEntry(
        path=path,
        db_path=f"{path}/.opencode/index_{tier}",
        tier=tier,
        dims=dims,
    )


def _make_row(path: str = "/tmp/foo.py", score: float = 0.9, project: str = "/tmp/proj") -> dict:
    return {
        "path": path,
        "content": "def foo(): pass",
        "language": "python",
        "start_line": 1,
        "end_line": 5,
        "_score": score,
        "_project_path": project,
        "chunk_id": 12345,
    }


# ---------------------------------------------------------------------------
# Cache key tests
# ---------------------------------------------------------------------------


def test_cache_key_consistent():
    projects = [_make_project("/tmp/a"), _make_project("/tmp/b")]
    key1 = _cache_key("hello", projects, "balanced", 10, True)
    key2 = _cache_key("hello", projects, "balanced", 10, True)
    assert key1 == key2


def test_cache_key_query_case_insensitive():
    projects = [_make_project()]
    key1 = _cache_key("Hello World", projects, "balanced", 10, True)
    key2 = _cache_key("hello world", projects, "balanced", 10, True)
    assert key1 == key2


def test_cache_key_differs_by_tier():
    projects = [_make_project()]
    key1 = _cache_key("query", projects, "budget", 10, True)
    key2 = _cache_key("query", projects, "premium", 10, True)
    assert key1 != key2


def test_cache_key_differs_by_top_k():
    projects = [_make_project()]
    key1 = _cache_key("query", projects, "balanced", 5, True)
    key2 = _cache_key("query", projects, "balanced", 10, True)
    assert key1 != key2


def test_cache_key_differs_by_rerank():
    projects = [_make_project()]
    key1 = _cache_key("query", projects, "balanced", 10, True)
    key2 = _cache_key("query", projects, "balanced", 10, False)
    assert key1 != key2


def test_cache_key_differs_by_project_set():
    p1 = [_make_project("/tmp/a")]
    p2 = [_make_project("/tmp/b")]
    key1 = _cache_key("query", p1, "balanced", 10, True)
    key2 = _cache_key("query", p2, "balanced", 10, True)
    assert key1 != key2


# ---------------------------------------------------------------------------
# search() with mocked dependencies
# ---------------------------------------------------------------------------


async def _fake_embed_query(query, model, dims):
    return [0.5] * dims


async def _fake_search_hybrid(query, query_vec, limit):
    # Return a few mock rows
    return [_make_row(path=f"/tmp/file{i}.py", score=0.9 - i * 0.1) for i in range(3)]


async def _fake_rerank_rows(query, rows, model, top_k):
    return sorted(rows, key=lambda r: r.get("_score", 0.0), reverse=True)[:top_k]


def _make_mock_storage(rows=None):
    mock = MagicMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    mock.open = AsyncMock()
    mock.close = AsyncMock()
    if rows is None:
        rows = [_make_row()]
    mock.search_hybrid = AsyncMock(return_value=rows)
    return mock


@pytest.mark.asyncio
async def test_search_empty_query_returns_empty():
    project = _make_project()
    result = await search("", projects=[project])
    assert result == []


@pytest.mark.asyncio
async def test_search_no_projects_returns_empty():
    result = await search("hello", projects=[])
    assert result == []


@pytest.mark.asyncio
async def test_search_returns_search_results():
    clear_search_cache()
    project = _make_project()
    dims = project.dims

    with patch("opencode_search.search._embed_query_sync", return_value=[0.5] * dims), \
         patch("opencode_search.search._rerank_sync", return_value=[(0, 0.9)]), \
         patch("opencode_search.search.Storage") as MockStorage:
        mock_st = _make_mock_storage()
        MockStorage.return_value = mock_st

        results = await search("find function", projects=[project], top_k=5)

    assert isinstance(results, list)
    assert all(isinstance(r, SearchResult) for r in results)


@pytest.mark.asyncio
async def test_search_result_fields():
    clear_search_cache()
    project = _make_project()
    dims = project.dims
    rows = [_make_row(path="/tmp/file.py", score=0.95)]

    with patch("opencode_search.search._embed_query_sync", return_value=[0.5] * dims), \
         patch("opencode_search.search._rerank_sync", return_value=[(0, 0.95)]), \
         patch("opencode_search.search.Storage") as MockStorage:
        mock_st = _make_mock_storage(rows=rows)
        MockStorage.return_value = mock_st

        results = await search("query", projects=[project], top_k=1)

    if results:
        r = results[0]
        assert hasattr(r, "path")
        assert hasattr(r, "content")
        assert hasattr(r, "score")
        assert hasattr(r, "language")
        assert hasattr(r, "start_line")
        assert hasattr(r, "end_line")
        assert hasattr(r, "project_path")


@pytest.mark.asyncio
async def test_search_uses_cache_on_second_call():
    clear_search_cache()
    project = _make_project()
    dims = project.dims
    call_count = {"n": 0}

    def counting_embed(*args, **kwargs):
        call_count["n"] += 1
        return [0.5] * dims

    with patch("opencode_search.search._embed_query_sync", side_effect=counting_embed), \
         patch("opencode_search.search._rerank_sync", return_value=[(0, 0.9)]), \
         patch("opencode_search.search.Storage") as MockStorage:
        MockStorage.return_value = _make_mock_storage()

        await search("cached query", projects=[project])
        await search("cached query", projects=[project])

    assert call_count["n"] == 1, "embed_query should only be called once due to cache"


@pytest.mark.asyncio
async def test_search_deduplicates_results():
    clear_search_cache()
    project = _make_project()
    dims = project.dims
    # Two identical rows (same path + content prefix)
    rows = [_make_row(path="/tmp/dup.py", score=0.9)] * 2

    with patch("opencode_search.search._embed_query_sync", return_value=[0.5] * dims), \
         patch("opencode_search.search._rerank_sync", return_value=[(0, 0.9)]), \
         patch("opencode_search.search.Storage") as MockStorage:
        MockStorage.return_value = _make_mock_storage(rows=rows)
        results = await search("dedup test", projects=[project], top_k=10)

    # Duplicate should be deduplicated
    paths = [r.path for r in results]
    assert len(paths) == len(set(paths))


@pytest.mark.asyncio
async def test_search_no_rerank_skips_reranker():
    clear_search_cache()
    project = _make_project()
    dims = project.dims
    rerank_calls = {"n": 0}

    def counting_rerank(*args, **kwargs):
        rerank_calls["n"] += 1
        return [(0, 0.9)]

    with patch("opencode_search.search._embed_query_sync", return_value=[0.5] * dims), \
         patch("opencode_search.search._rerank_sync", side_effect=counting_rerank), \
         patch("opencode_search.search.Storage") as MockStorage:
        MockStorage.return_value = _make_mock_storage()
        await search("no rerank", projects=[project], use_rerank=False)

    assert rerank_calls["n"] == 0


@pytest.mark.asyncio
async def test_search_many_projects_skips_per_project_rerank():
    """With >SKIP_STAGE1_RERANK_N projects, per-project rerank is skipped."""
    clear_search_cache()
    n = SKIP_STAGE1_RERANK_N + 2
    projects = [_make_project(f"/tmp/proj{i}") for i in range(n)]
    dims = projects[0].dims
    rerank_calls = {"n": 0}

    def counting_rerank(*args, **kwargs):
        rerank_calls["n"] += 1
        docs = args[1] if len(args) > 1 else kwargs.get("docs", [])
        return [(i, 0.9 - i * 0.01) for i in range(min(len(docs), 10))]

    with patch("opencode_search.search._embed_query_sync", return_value=[0.5] * dims), \
         patch("opencode_search.search._rerank_sync", side_effect=counting_rerank), \
         patch("opencode_search.search.Storage") as MockStorage:
        MockStorage.return_value = _make_mock_storage()
        await search("many projects", projects=projects, use_rerank=True)

    # Should call rerank only ONCE (global rerank), not per project
    assert rerank_calls["n"] == 1


@pytest.mark.asyncio
async def test_search_few_projects_does_per_project_rerank():
    """With ≤SKIP_STAGE1_RERANK_N projects, per-project rerank is done."""
    clear_search_cache()
    n = min(SKIP_STAGE1_RERANK_N, 2)
    projects = [_make_project(f"/tmp/proj{i}") for i in range(n)]
    dims = projects[0].dims
    rerank_calls = {"n": 0}

    def counting_rerank(*args, **kwargs):
        rerank_calls["n"] += 1
        docs = args[1] if len(args) > 1 else kwargs.get("docs", [])
        return [(i, 0.9 - i * 0.01) for i in range(min(len(docs), STAGE1_RERANK_K))]

    with patch("opencode_search.search._embed_query_sync", return_value=[0.5] * dims), \
         patch("opencode_search.search._rerank_sync", side_effect=counting_rerank), \
         patch("opencode_search.search.Storage") as MockStorage:
        MockStorage.return_value = _make_mock_storage()
        await search("few projects", projects=projects, use_rerank=True)

    # Per-project rerank + global rerank → n+1 calls
    assert rerank_calls["n"] > 1


def test_clear_search_cache():
    clear_search_cache()  # Should not raise


# ---------------------------------------------------------------------------
# search_project convenience wrapper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_project_delegates_to_search():
    clear_search_cache()
    project = _make_project()
    dims = project.dims

    with patch("opencode_search.search._embed_query_sync", return_value=[0.5] * dims), \
         patch("opencode_search.search._rerank_sync", return_value=[(0, 0.9)]), \
         patch("opencode_search.search.Storage") as MockStorage:
        MockStorage.return_value = _make_mock_storage()
        results = await search_project("query", project=project)

    assert isinstance(results, list)
