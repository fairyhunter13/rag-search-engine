"""Tests for indexer core, LanceDB storage, cleaner, and compaction."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

pytest.importorskip("lancedb")
pytest.importorskip("pyarrow")

import pyarrow as pa

from opencode_search.cleaner import remove_chunks_for_paths, remove_stale_chunks
from opencode_search.compaction import (
    COMPACTION_THRESHOLD_OPS,
    compact_if_needed,
    force_compact,
)
from opencode_search.config import DEFAULT_DIMS
from opencode_search.indexer import (
    IndexResult,
    _hash_file,
    _make_chunk_id,
    _read_file,
    index_file,
    index_files,
    index_project,
)
from opencode_search.storage import ChunkData, Storage, build_schema

pytestmark = [pytest.mark.integration, pytest.mark.runtime_deps]


# === Schema ===

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
    vtype = vector_field.type
    assert hasattr(vtype, "list_size")
    assert vtype.list_size == 384


def test_build_schema_vector_dims_768():
    schema = build_schema(768)
    assert schema.field("vector").type.list_size == 768


def test_build_schema_field_types():
    schema = build_schema(512)
    assert pa.types.is_int64(schema.field("chunk_id").type)
    path_type = schema.field("path").type
    assert pa.types.is_string(path_type) or pa.types.is_large_string(path_type)
    assert pa.types.is_int32(schema.field("start_line").type)
    assert pa.types.is_timestamp(schema.field("created_at").type)


def test_build_schema_11_fields():
    assert len(build_schema(512)) == 11


# === ChunkData ===

def test_chunk_data_creation():
    chunk = ChunkData(
        chunk_id=12345, path="/tmp/foo.py", file_hash="abc123", language="python",
        position=0, content="def foo(): pass", content_hash="def456",
        start_line=1, end_line=1, vector=[0.1] * 512, created_at=time.time(),
    )
    assert chunk.chunk_id == 12345
    assert chunk.language == "python"
    assert len(chunk.vector) == 512


def test_storage_sql_quote_escapes_single_quotes():
    assert Storage._sql_quote("/tmp/bob's/file.py") == "'/tmp/bob''s/file.py'"


# === Storage CRUD ===

@pytest_asyncio.fixture
async def storage(tmp_path):
    s = Storage(db_path=str(tmp_path / "testdb"), dims=384)
    await s.open()
    yield s
    await s.close()


async def _make_chunk(path: str = "/tmp/test.py", pos: int = 0, dims: int = 384) -> ChunkData:
    return ChunkData(
        chunk_id=abs(hash(f"{path}:{pos}")), path=path, file_hash="aabbcc", language="python",
        position=pos, content=f"def func_{pos}(): pass", content_hash="ddeeff",
        start_line=pos * 10, end_line=pos * 10 + 5,
        vector=[float(pos % 255) / 255.0] * dims, created_at=time.time(),
    )


@pytest.mark.asyncio
async def test_storage_open_creates_table(tmp_path):
    s = Storage(db_path=str(tmp_path / "db"), dims=384)
    await s.open()
    assert await s.count() == 0
    await s.close()


@pytest.mark.asyncio
async def test_storage_write_and_count(storage):
    await storage.write_chunks([await _make_chunk(pos=i) for i in range(5)])
    assert await storage.count() == 5


@pytest.mark.asyncio
async def test_storage_upsert_deduplication(storage):
    chunk = await _make_chunk(pos=0)
    await storage.write_chunks([chunk])
    chunk2 = await _make_chunk(pos=0)
    chunk2.content = "updated content"
    await storage.write_chunks([chunk2])
    assert await storage.count() == 1


@pytest.mark.asyncio
async def test_storage_get_file_hashes(storage):
    chunks = [
        await _make_chunk(path="/tmp/a.py", pos=0),
        await _make_chunk(path="/tmp/a.py", pos=1),
        await _make_chunk(path="/tmp/b.py", pos=0),
    ]
    await storage.write_chunks(chunks)
    hashes = await storage.get_file_hashes()
    assert "/tmp/a.py" in hashes and "/tmp/b.py" in hashes
    assert hashes["/tmp/a.py"] == "aabbcc"


@pytest.mark.asyncio
async def test_storage_delete_by_path(storage):
    await storage.write_chunks([
        await _make_chunk(path="/tmp/del.py", pos=0),
        await _make_chunk(path="/tmp/keep.py", pos=0),
    ])
    await storage.delete_by_path("/tmp/del.py")
    assert await storage.count() == 1
    hashes = await storage.get_file_hashes()
    assert "/tmp/del.py" not in hashes and "/tmp/keep.py" in hashes


@pytest.mark.asyncio
async def test_storage_delete_by_paths(storage):
    chunks = [await _make_chunk(path=f"/tmp/file{i}.py", pos=0) for i in range(4)]
    await storage.write_chunks(chunks)
    await storage.delete_by_paths(["/tmp/file0.py", "/tmp/file1.py"])
    assert await storage.count() == 2


@pytest.mark.asyncio
async def test_storage_vector_search(storage):
    await storage.write_chunks([await _make_chunk(pos=i) for i in range(10)])
    results = await storage.search_vector([0.5] * 384, limit=5)
    assert len(results) <= 5
    for r in results:
        assert "_score" in r


@pytest.mark.asyncio
async def test_storage_search_hybrid_returns_merged(storage):
    await storage.write_chunks([await _make_chunk(pos=i) for i in range(5)])
    assert isinstance(await storage.search_hybrid("func", [0.5] * 384, limit=5), list)


@pytest.mark.asyncio
async def test_storage_search_hybrid_keeps_multiple_chunks_same_path(storage):
    await storage.write_chunks([
        await _make_chunk(path="/tmp/same.py", pos=0),
        await _make_chunk(path="/tmp/same.py", pos=1),
    ])
    results = await storage.search_hybrid("func", [0.5] * 384, limit=5)
    assert {0, 1}.issubset({r.get("position") for r in results})


@pytest.mark.asyncio
async def test_storage_compact(storage):
    await storage.write_chunks([await _make_chunk(pos=i) for i in range(3)])
    await storage.compact()


@pytest.mark.asyncio
async def test_storage_get_set_config(storage):
    storage.set_config("schema_version", "2")
    assert storage.get_config("schema_version") == "2"


@pytest.mark.asyncio
async def test_storage_get_config_missing(storage):
    assert storage.get_config("nonexistent_key") is None


# === Cleaner ===

@pytest_asyncio.fixture
async def cleaner_storage(tmp_path):
    s = Storage(db_path=str(tmp_path / "db"), dims=384)
    await s.open()
    yield s
    await s.close()


async def _seed_chunks(storage: Storage, paths: list[str]) -> None:
    chunks = []
    for p in paths:
        chunks.append(ChunkData(
            chunk_id=abs(hash(p)), path=p, file_hash="hh", language="python",
            position=0, content="x", content_hash="cc", start_line=0, end_line=0,
            vector=[0.1] * 384, created_at=int(time.time() * 1_000_000),
        ))
    await storage.write_chunks(chunks)


@pytest.mark.asyncio
async def test_remove_stale_no_stale(cleaner_storage):
    await _seed_chunks(cleaner_storage, ["/tmp/a.py", "/tmp/b.py"])
    removed = await remove_stale_chunks(cleaner_storage, current_paths={"/tmp/a.py", "/tmp/b.py"})
    assert removed == 0 and await cleaner_storage.count() == 2


@pytest.mark.asyncio
async def test_remove_stale_some_stale(cleaner_storage):
    await _seed_chunks(cleaner_storage, ["/tmp/a.py", "/tmp/b.py", "/tmp/c.py"])
    removed = await remove_stale_chunks(cleaner_storage, current_paths={"/tmp/a.py"})
    assert removed == 2 and await cleaner_storage.count() == 1


@pytest.mark.asyncio
async def test_remove_stale_all_stale(cleaner_storage):
    await _seed_chunks(cleaner_storage, ["/tmp/a.py"])
    removed = await remove_stale_chunks(cleaner_storage, current_paths=set())
    assert removed == 1 and await cleaner_storage.count() == 0


@pytest.mark.asyncio
async def test_remove_stale_empty_storage(cleaner_storage):
    assert await remove_stale_chunks(cleaner_storage, current_paths={"/tmp/anything.py"}) == 0


@pytest.mark.asyncio
async def test_remove_chunks_for_paths(cleaner_storage):
    await _seed_chunks(cleaner_storage, ["/tmp/a.py", "/tmp/b.py", "/tmp/c.py"])
    await remove_chunks_for_paths(cleaner_storage, ["/tmp/b.py"])
    assert await cleaner_storage.count() == 2
    hashes = await cleaner_storage.get_file_hashes()
    assert "/tmp/b.py" not in hashes and "/tmp/a.py" in hashes


@pytest.mark.asyncio
async def test_remove_chunks_empty_list(cleaner_storage):
    await _seed_chunks(cleaner_storage, ["/tmp/a.py"])
    await remove_chunks_for_paths(cleaner_storage, [])
    assert await cleaner_storage.count() == 1


# === Compaction ===

@pytest_asyncio.fixture
async def compact_storage(tmp_path):
    s = Storage(db_path=str(tmp_path / "db"), dims=384)
    await s.open()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_compact_if_needed_below_threshold(compact_storage):
    assert await compact_if_needed(compact_storage, ops_since_last=COMPACTION_THRESHOLD_OPS - 1) is False


@pytest.mark.asyncio
async def test_compact_if_needed_at_threshold(compact_storage):
    assert await compact_if_needed(compact_storage, ops_since_last=COMPACTION_THRESHOLD_OPS) is True


@pytest.mark.asyncio
async def test_compact_if_needed_above_threshold(compact_storage):
    assert await compact_if_needed(compact_storage, ops_since_last=COMPACTION_THRESHOLD_OPS * 2) is True


@pytest.mark.asyncio
async def test_compact_if_needed_uses_storage_compact():
    mock_storage = AsyncMock()
    mock_storage.compact = AsyncMock()
    result = await compact_if_needed(mock_storage, ops_since_last=COMPACTION_THRESHOLD_OPS)
    assert result is True
    mock_storage.compact.assert_awaited_once()


@pytest.mark.asyncio
async def test_compact_if_needed_handles_compact_exception():
    mock_storage = AsyncMock()
    mock_storage.compact = AsyncMock(side_effect=RuntimeError("LanceDB error"))
    assert await compact_if_needed(mock_storage, ops_since_last=COMPACTION_THRESHOLD_OPS) is False


@pytest.mark.asyncio
async def test_force_compact_success(compact_storage):
    assert (await force_compact(compact_storage))["status"] == "ok"


@pytest.mark.asyncio
async def test_force_compact_handles_exception():
    mock_storage = AsyncMock()
    mock_storage.compact = AsyncMock(side_effect=RuntimeError("oops"))
    result = await force_compact(mock_storage)
    assert result["status"] == "error" and "oops" in result["error"]


@pytest.mark.asyncio
async def test_force_compact_calls_storage_compact():
    mock_storage = AsyncMock()
    mock_storage.compact = AsyncMock()
    await force_compact(mock_storage)
    mock_storage.compact.assert_awaited_once()


# === Core Indexer ===

def test_hash_file_deterministic(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hello world")
    assert _hash_file(f) == _hash_file(f)
    assert len(_hash_file(f)) == 64


def test_hash_file_changes_with_content(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hello")
    h1 = _hash_file(f)
    f.write_text("world")
    assert _hash_file(f) != h1


def test_read_file_text(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hello\n")
    assert _read_file(f) == "hello\n"


def test_read_file_missing(tmp_path):
    assert _read_file(tmp_path / "nonexistent.txt") is None


def test_make_chunk_id_stable():
    assert _make_chunk_id("/tmp/foo.py", 0) == _make_chunk_id("/tmp/foo.py", 0)


def test_make_chunk_id_differs_by_position():
    assert _make_chunk_id("/tmp/foo.py", 0) != _make_chunk_id("/tmp/foo.py", 1)


def test_make_chunk_id_differs_by_path():
    assert _make_chunk_id("/tmp/foo.py", 0) != _make_chunk_id("/tmp/bar.py", 0)


def test_make_chunk_id_fits_int64():
    assert 0 <= _make_chunk_id("/some/very/long/path/file.py", 12345) < 2**62


def test_index_result_creation():
    r = IndexResult(files_indexed=3, files_unchanged=1, files_removed=0, chunks_total=10, errors=0, elapsed_s=0.5)
    assert r.files_indexed == 3 and r.chunks_total == 10


@pytest_asyncio.fixture
async def real_storage(tmp_path):
    s = Storage(db_path=str(tmp_path / "db"), dims=DEFAULT_DIMS)
    await s.open()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_index_file_unchanged(real_storage, tmp_path):
    f = tmp_path / "code.py"
    f.write_text("def hello(): pass\n")
    existing = {str(f): _hash_file(f)}
    with patch("opencode_search.chunker.chunk_file") as mock_chunk:
        result = await index_file(real_storage, f, existing_hashes=existing)
    assert result["status"] == "unchanged" and result["chunks"] == 0
    mock_chunk.assert_not_called()


@pytest.mark.asyncio
async def test_index_file_unreadable_returns_skipped(real_storage, tmp_path):
    f = tmp_path / "bad.dat"
    f.write_bytes(b"\xff\xfe garbage")
    with patch("opencode_search.indexer._read_file", return_value=None):
        result = await index_file(real_storage, f, existing_hashes={})
    assert result["status"] == "skipped"


@pytest.mark.asyncio
async def test_index_file_empty_chunks_returns_empty(real_storage, tmp_path):
    f = tmp_path / "small.py"
    f.write_text("x = 1")
    with patch("opencode_search.chunker.chunk_file", return_value=[]):
        result = await index_file(real_storage, f, existing_hashes={})
    assert result["status"] == "empty"


@pytest.mark.asyncio
async def test_index_file_success(real_storage, tmp_path):
    from opencode_search.chunker import Chunk

    f = tmp_path / "code.py"
    f.write_text("def hello():\n    return 'world'\n")
    fake_chunks = [Chunk(content="def hello(): pass", start_line=1, end_line=2, chunk_type="code", language="python")]
    with patch("opencode_search.chunker.chunk_file", return_value=fake_chunks), \
         patch("opencode_search.embeddings.embed_passages", return_value=[[0.1] * DEFAULT_DIMS]):
        result = await index_file(real_storage, f, existing_hashes={})
    assert result["status"] == "indexed" and result["chunks"] == 1
    assert await real_storage.count() == 1


@pytest.mark.asyncio
async def test_index_file_replaces_stale_chunks_for_path(real_storage, tmp_path):
    from opencode_search.chunker import Chunk

    f = tmp_path / "code.py"
    f.write_text("def hello():\n    return 'world'\n")
    await real_storage.write_chunks([
        ChunkData(chunk_id=_make_chunk_id(str(f), i), path=str(f), file_hash="old", language="python",
                  position=i, content=f"old chunk {i}", content_hash=f"old{i}", start_line=i + 1,
                  end_line=i + 1, vector=[0.0] * DEFAULT_DIMS, created_at=1)
        for i in range(3)
    ])
    fake_chunks = [Chunk(content="def hello(): pass", start_line=1, end_line=1, chunk_type="code", language="python")]
    with patch("opencode_search.chunker.chunk_file", return_value=fake_chunks), \
         patch("opencode_search.embeddings.embed_passages", return_value=[[0.1] * DEFAULT_DIMS]):
        result = await index_file(real_storage, f, existing_hashes={})
    assert result["status"] == "indexed" and await real_storage.count() == 1


@pytest.mark.asyncio
async def test_index_file_vector_count_mismatch_is_error(real_storage, tmp_path):
    from opencode_search.chunker import Chunk

    f = tmp_path / "code.py"
    f.write_text("x = 1\n")
    fake_chunks = [
        Chunk(content="x = 1", start_line=1, end_line=1, chunk_type="code", language="python"),
        Chunk(content="y = 2", start_line=2, end_line=2, chunk_type="code", language="python"),
    ]
    with patch("opencode_search.chunker.chunk_file", return_value=fake_chunks), \
         patch("opencode_search.embeddings.embed_passages", return_value=[[0.1] * DEFAULT_DIMS]):
        result = await index_file(real_storage, f, existing_hashes={})
    assert result["status"] == "error" and await real_storage.count() == 0


@pytest.mark.asyncio
async def test_index_file_hash_error_returns_error(real_storage, tmp_path):
    f = tmp_path / "missing.py"
    result = await index_file(real_storage, f, existing_hashes={})
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_index_project_empty_dir(real_storage, tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    result = await index_project(real_storage, project_root)
    assert isinstance(result, IndexResult) and result.files_indexed == 0 and result.chunks_total == 0


@pytest.mark.asyncio
async def test_index_project_indexes_files(real_storage, tmp_path):
    from opencode_search.chunker import Chunk

    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "a.py").write_text("def a(): pass\n")
    (project_root / "b.py").write_text("def b(): pass\n")

    with patch("opencode_search.chunker.chunk_file",
               side_effect=lambda content, path: [Chunk(content=content, start_line=0, end_line=1, chunk_type="code", language="python")]), \
         patch("opencode_search.embeddings.embed_passages",
               side_effect=lambda texts, **kw: [[0.5] * DEFAULT_DIMS] * len(texts)):
        result = await index_project(real_storage, project_root)
    assert result.files_indexed == 2 and result.chunks_total == 2


@pytest.mark.asyncio
async def test_index_project_progress_callback(real_storage, tmp_path):
    from opencode_search.chunker import Chunk

    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "a.py").write_text("x = 1\n")

    calls = []
    async def cb(idx, total, path):
        calls.append((idx, total, path))

    with patch("opencode_search.chunker.chunk_file",
               return_value=[Chunk(content="x=1", start_line=0, end_line=0, chunk_type="code", language="python")]), \
         patch("opencode_search.embeddings.embed_passages",
               side_effect=lambda texts, **kw: [[0.1] * DEFAULT_DIMS] * len(texts)):
        await index_project(real_storage, project_root, progress_callback=cb)
    assert len(calls) >= 1 and calls[0][1] == 1


@pytest.mark.asyncio
async def test_index_project_loads_existing_hashes_once(real_storage, tmp_path):
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
               return_value=[Chunk(content="x", start_line=0, end_line=0, chunk_type="code", language="python")]), \
         patch("opencode_search.embeddings.embed_passages",
               side_effect=lambda texts, **kw: [[0.1] * DEFAULT_DIMS] * len(texts)):
        await index_project(real_storage, tmp_path)
    assert call_count["n"] <= 2


@pytest.mark.asyncio
async def test_index_files_empty_list(real_storage):
    assert (await index_files(real_storage, [])).files_indexed == 0


@pytest.mark.asyncio
async def test_index_files_with_paths(real_storage, tmp_path):
    from opencode_search.chunker import Chunk

    paths = []
    for i in range(2):
        p = tmp_path / f"f{i}.py"
        p.write_text(f"x = {i}\n")
        paths.append(p)

    with patch("opencode_search.chunker.chunk_file",
               return_value=[Chunk(content="x", start_line=0, end_line=0, chunk_type="code", language="python")]), \
         patch("opencode_search.embeddings.embed_passages", return_value=[[0.1] * DEFAULT_DIMS]):
        result = await index_files(real_storage, paths)
    assert result.files_indexed == 2
