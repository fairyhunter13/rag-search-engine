"""Tests for opencode_search.storage — schema building and Storage CRUD.

Storage tests that require a real LanceDB table use tmp_path fixtures.
GPU tests (actual embedding inference) are @pytest.mark.gpu.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pyarrow as pa
import pytest

from opencode_search.storage import ChunkData, Storage, build_schema


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


def test_build_schema_field_names():
    schema = build_schema(768)
    names = schema.names
    assert "chunk_id" in names
    assert "path" in names
    assert "file_hash" in names
    assert "language" in names
    assert "position" in names
    assert "content" in names
    assert "content_hash" in names
    assert "start_line" in names
    assert "end_line" in names
    assert "vector" in names
    assert "created_at" in names


def test_build_schema_vector_dims_384():
    schema = build_schema(384)
    vector_field = schema.field("vector")
    # LanceDB uses FixedSizeList for fixed-dimension vectors
    vtype = vector_field.type
    assert hasattr(vtype, "list_size"), f"Expected fixed-size list, got {vtype}"
    assert vtype.list_size == 384


def test_build_schema_vector_dims_768():
    schema = build_schema(768)
    vector_field = schema.field("vector")
    assert vector_field.type.list_size == 768


def test_build_schema_field_types():
    schema = build_schema(512)
    assert pa.types.is_int64(schema.field("chunk_id").type)
    path_type = schema.field("path").type
    assert pa.types.is_string(path_type) or pa.types.is_large_string(path_type)
    assert pa.types.is_int32(schema.field("start_line").type)
    assert pa.types.is_int32(schema.field("end_line").type)
    assert pa.types.is_timestamp(schema.field("created_at").type)


def test_build_schema_11_fields():
    schema = build_schema(512)
    assert len(schema) == 11


# ---------------------------------------------------------------------------
# ChunkData dataclass
# ---------------------------------------------------------------------------


def test_chunk_data_creation():
    chunk = ChunkData(
        chunk_id=12345,
        path="/tmp/foo.py",
        file_hash="abc123",
        language="python",
        position=0,
        content="def foo(): pass",
        content_hash="def456",
        start_line=1,
        end_line=1,
        vector=[0.1] * 512,
        created_at=time.time(),
    )
    assert chunk.chunk_id == 12345
    assert chunk.language == "python"
    assert len(chunk.vector) == 512


# ---------------------------------------------------------------------------
# Storage CRUD (real LanceDB with temp dir)
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage(tmp_path):
    s = Storage(db_path=str(tmp_path / "testdb"), dims=384)
    await s.open()
    yield s
    await s.close()


async def _make_chunk(path: str = "/tmp/test.py", pos: int = 0, dims: int = 384) -> ChunkData:
    return ChunkData(
        chunk_id=abs(hash(f"{path}:{pos}")),
        path=path,
        file_hash="aabbcc",
        language="python",
        position=pos,
        content=f"def func_{pos}(): pass",
        content_hash="ddeeff",
        start_line=pos * 10,
        end_line=pos * 10 + 5,
        vector=[float(pos % 255) / 255.0] * dims,
        created_at=time.time(),
    )


@pytest.mark.asyncio
async def test_storage_open_creates_table(tmp_path):
    s = Storage(db_path=str(tmp_path / "db"), dims=384)
    await s.open()
    count = await s.count()
    assert count == 0
    await s.close()


@pytest.mark.asyncio
async def test_storage_write_and_count(storage):
    chunks = [await _make_chunk(pos=i) for i in range(5)]
    await storage.write_chunks(chunks)
    count = await storage.count()
    assert count == 5


@pytest.mark.asyncio
async def test_storage_upsert_deduplication(storage):
    chunk = await _make_chunk(pos=0)
    await storage.write_chunks([chunk])
    # Write same chunk again — should upsert (not duplicate)
    chunk2 = await _make_chunk(pos=0)
    chunk2.content = "updated content"
    await storage.write_chunks([chunk2])
    count = await storage.count()
    assert count == 1


@pytest.mark.asyncio
async def test_storage_get_file_hashes(storage):
    chunks = [
        await _make_chunk(path="/tmp/a.py", pos=0),
        await _make_chunk(path="/tmp/a.py", pos=1),
        await _make_chunk(path="/tmp/b.py", pos=0),
    ]
    await storage.write_chunks(chunks)
    hashes = await storage.get_file_hashes()
    assert "/tmp/a.py" in hashes
    assert "/tmp/b.py" in hashes
    assert hashes["/tmp/a.py"] == "aabbcc"


@pytest.mark.asyncio
async def test_storage_delete_by_path(storage):
    chunks = [
        await _make_chunk(path="/tmp/del.py", pos=0),
        await _make_chunk(path="/tmp/keep.py", pos=0),
    ]
    await storage.write_chunks(chunks)
    await storage.delete_by_path("/tmp/del.py")
    count = await storage.count()
    assert count == 1
    hashes = await storage.get_file_hashes()
    assert "/tmp/del.py" not in hashes
    assert "/tmp/keep.py" in hashes


@pytest.mark.asyncio
async def test_storage_delete_by_paths(storage):
    chunks = [
        await _make_chunk(path=f"/tmp/file{i}.py", pos=0)
        for i in range(4)
    ]
    await storage.write_chunks(chunks)
    await storage.delete_by_paths(["/tmp/file0.py", "/tmp/file1.py"])
    count = await storage.count()
    assert count == 2


@pytest.mark.asyncio
async def test_storage_vector_search(storage):
    chunks = [await _make_chunk(pos=i) for i in range(10)]
    await storage.write_chunks(chunks)
    query_vec = [0.5] * 384
    results = await storage.search_vector(query_vec, limit=5)
    assert len(results) <= 5
    # All results have _score
    for r in results:
        assert "_score" in r


@pytest.mark.asyncio
async def test_storage_search_hybrid_returns_merged(storage):
    chunks = [await _make_chunk(pos=i) for i in range(5)]
    await storage.write_chunks(chunks)
    query_vec = [0.5] * 384
    results = await storage.search_hybrid("func", query_vec, limit=5)
    assert isinstance(results, list)


@pytest.mark.asyncio
async def test_storage_compact(storage):
    chunks = [await _make_chunk(pos=i) for i in range(3)]
    await storage.write_chunks(chunks)
    # Should not raise
    await storage.compact()


@pytest.mark.asyncio
async def test_storage_get_set_config(storage):
    storage.set_config("schema_version", "2")
    val = storage.get_config("schema_version")
    assert val == "2"


@pytest.mark.asyncio
async def test_storage_get_config_missing(storage):
    val = storage.get_config("nonexistent_key")
    assert val is None
