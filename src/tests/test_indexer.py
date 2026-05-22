"""Tests for opencode_search.indexer — file/project indexing pipeline.

Embed_passages is mocked out so no GPU is required.
@pytest.mark.gpu tests run the real GPU pipeline (skipped without CUDA).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opencode_search.indexer import (
    IndexResult,
    _hash_file,
    _make_chunk_id,
    _read_file,
    index_file,
    index_files,
    index_project,
)
from opencode_search.storage import Storage


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


def test_hash_file_deterministic(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hello world")
    h1 = _hash_file(f)
    h2 = _hash_file(f)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_hash_file_changes_with_content(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hello")
    h1 = _hash_file(f)
    f.write_text("world")
    h2 = _hash_file(f)
    assert h1 != h2


def test_read_file_text(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hello\n")
    assert _read_file(f) == "hello\n"


def test_read_file_missing(tmp_path):
    f = tmp_path / "nonexistent.txt"
    assert _read_file(f) is None


def test_make_chunk_id_stable():
    a = _make_chunk_id("/tmp/foo.py", 0)
    b = _make_chunk_id("/tmp/foo.py", 0)
    assert a == b


def test_make_chunk_id_differs_by_position():
    a = _make_chunk_id("/tmp/foo.py", 0)
    b = _make_chunk_id("/tmp/foo.py", 1)
    assert a != b


def test_make_chunk_id_differs_by_path():
    a = _make_chunk_id("/tmp/foo.py", 0)
    b = _make_chunk_id("/tmp/bar.py", 0)
    assert a != b


def test_make_chunk_id_fits_int64():
    chunk_id = _make_chunk_id("/some/very/long/path/file.py", 12345)
    assert 0 <= chunk_id < 2**62  # fits comfortably in int64


# ---------------------------------------------------------------------------
# IndexResult dataclass
# ---------------------------------------------------------------------------


def test_index_result_creation():
    r = IndexResult(files_indexed=3, files_unchanged=1, files_removed=0,
                    chunks_total=10, errors=0, elapsed_s=0.5)
    assert r.files_indexed == 3
    assert r.chunks_total == 10


# ---------------------------------------------------------------------------
# index_file
# ---------------------------------------------------------------------------


@pytest.fixture
async def real_storage(tmp_path):
    # Use budget-tier dims (512) so embeddings produced by the budget tier fit.
    # DB lives in a sibling of the project_root subdir used by tests so that
    # iter_files() doesn't walk into LanceDB internals.
    from opencode_search.config import get_tier_dims
    s = Storage(db_path=str(tmp_path / "db"), dims=get_tier_dims("budget"))
    await s.open()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_index_file_unchanged(real_storage, tmp_path):
    """If file_hash matches existing_hashes, file is skipped."""
    f = tmp_path / "code.py"
    f.write_text("def hello(): pass\n")
    file_hash = _hash_file(f)
    existing = {str(f): file_hash}

    # Should not call embeddings or chunker
    with patch("opencode_search.chunker.chunk_file") as mock_chunk:
        result = await index_file(
            real_storage, f, tier="budget",
            existing_hashes=existing,
        )

    assert result["status"] == "unchanged"
    assert result["chunks"] == 0
    mock_chunk.assert_not_called()


@pytest.mark.asyncio
async def test_index_file_unreadable_returns_skipped(real_storage, tmp_path):
    """A binary/unreadable file should return status='skipped'."""
    f = tmp_path / "bad.dat"
    f.write_bytes(b"\xff\xfe garbage")

    with patch("opencode_search.indexer._read_file", return_value=None):
        result = await index_file(
            real_storage, f, tier="budget",
            existing_hashes={},
        )

    assert result["status"] == "skipped"


@pytest.mark.asyncio
async def test_index_file_empty_chunks_returns_empty(real_storage, tmp_path):
    """If chunk_file returns [], we report 'empty'."""
    f = tmp_path / "small.py"
    f.write_text("x = 1")

    with patch("opencode_search.chunker.chunk_file", return_value=[]):
        result = await index_file(
            real_storage, f, tier="budget",
            existing_hashes={},
        )

    assert result["status"] == "empty"


@pytest.mark.asyncio
async def test_index_file_success(real_storage, tmp_path):
    """Happy path: file is hashed, chunked, embedded, and written."""
    from opencode_search.chunker import Chunk

    f = tmp_path / "code.py"
    f.write_text("def hello():\n    return 'world'\n")

    fake_chunks = [Chunk(content="def hello(): pass", start_line=1, end_line=2,
                        chunk_type="code", language="python")]
    fake_vectors = [[0.1] * 512]

    with patch("opencode_search.chunker.chunk_file", return_value=fake_chunks), \
         patch("opencode_search.embeddings.embed_passages", return_value=fake_vectors):
        result = await index_file(
            real_storage, f, tier="budget",
            existing_hashes={},
        )

    assert result["status"] == "indexed"
    assert result["chunks"] == 1

    # Verify it was stored
    count = await real_storage.count()
    assert count == 1


@pytest.mark.asyncio
async def test_index_file_hash_error_returns_error(real_storage, tmp_path):
    f = tmp_path / "missing.py"  # never created
    # _hash_file will raise FileNotFoundError; wrapped in error status
    result = await index_file(real_storage, f, tier="budget", existing_hashes={})
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# index_project
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_project_empty_dir(real_storage, tmp_path):
    """Indexing an empty dir produces an IndexResult with zero counts."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    result = await index_project(real_storage, project_root, tier="budget")
    assert isinstance(result, IndexResult)
    assert result.files_indexed == 0
    assert result.chunks_total == 0


@pytest.mark.asyncio
async def test_index_project_indexes_files(real_storage, tmp_path):
    """A directory with a couple of files yields files_indexed > 0."""
    from opencode_search.chunker import Chunk

    # Use a dedicated project subdir so we don't pick up LanceDB's db/ files.
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "a.py").write_text("def a(): pass\n")
    (project_root / "b.py").write_text("def b(): pass\n")

    def fake_chunk(content, path):
        return [Chunk(content=content, start_line=0, end_line=1,
                      chunk_type="code", language="python")]

    fake_vectors = [[0.5] * 512]

    with patch("opencode_search.chunker.chunk_file", side_effect=fake_chunk), \
         patch("opencode_search.embeddings.embed_passages", return_value=fake_vectors):
        result = await index_project(real_storage, project_root, tier="budget")

    assert result.files_indexed == 2
    assert result.chunks_total == 2


@pytest.mark.asyncio
async def test_index_project_progress_callback(real_storage, tmp_path):
    """progress_callback receives (idx, total, path) for each file."""
    from opencode_search.chunker import Chunk

    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "a.py").write_text("x = 1\n")

    calls = []

    async def cb(idx, total, path):
        calls.append((idx, total, path))

    with patch("opencode_search.chunker.chunk_file",
               return_value=[Chunk(content="x=1", start_line=0, end_line=0,
                                   chunk_type="code", language="python")]), \
         patch("opencode_search.embeddings.embed_passages", return_value=[[0.1] * 512]):
        await index_project(real_storage, project_root, tier="budget", progress_callback=cb)

    assert len(calls) >= 1
    assert calls[0][1] == 1  # total = 1 file


@pytest.mark.asyncio
async def test_index_project_loads_existing_hashes_once(real_storage, tmp_path):
    """Verify get_file_hashes is called once per index_project run, not per file."""
    from opencode_search.chunker import Chunk

    for i in range(3):
        (tmp_path / f"f{i}.py").write_text(f"x = {i}\n")

    original = real_storage.get_file_hashes
    call_count = {"n": 0}

    async def counting_get_hashes():
        call_count["n"] += 1
        return await original()

    real_storage.get_file_hashes = counting_get_hashes

    with patch("opencode_search.chunker.chunk_file",
               return_value=[Chunk(content="x", start_line=0, end_line=0,
                                   chunk_type="code", language="python")]), \
         patch("opencode_search.embeddings.embed_passages", return_value=[[0.1] * 512]):
        await index_project(real_storage, tmp_path, tier="budget")

    # Should be called once for skip detection + once for stale cleanup = 2 max
    assert call_count["n"] <= 2, (
        f"get_file_hashes called {call_count['n']} times; expected ≤2"
    )


# ---------------------------------------------------------------------------
# index_files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_files_empty_list(real_storage):
    result = await index_files(real_storage, [], tier="budget")
    assert result.files_indexed == 0


@pytest.mark.asyncio
async def test_index_files_with_paths(real_storage, tmp_path):
    from opencode_search.chunker import Chunk

    paths = []
    for i in range(2):
        p = tmp_path / f"f{i}.py"
        p.write_text(f"x = {i}\n")
        paths.append(p)

    with patch("opencode_search.chunker.chunk_file",
               return_value=[Chunk(content="x", start_line=0, end_line=0,
                                   chunk_type="code", language="python")]), \
         patch("opencode_search.embeddings.embed_passages", return_value=[[0.1] * 512]):
        result = await index_files(real_storage, paths, tier="budget")

    assert result.files_indexed == 2
