"""Tests for opencode_search.cleaner — stale chunk removal."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("lancedb")
pytest.importorskip("pyarrow")
import pytest_asyncio

from opencode_search.cleaner import remove_chunks_for_paths, remove_stale_chunks
from opencode_search.storage import ChunkData, Storage

pytestmark = [pytest.mark.integration, pytest.mark.runtime_deps]


@pytest_asyncio.fixture
async def storage(tmp_path):
    s = Storage(db_path=str(tmp_path / "db"), dims=384)
    await s.open()
    yield s
    await s.close()


async def _seed_chunks(storage: Storage, paths: list[str]) -> None:
    chunks = []
    for p in paths:
        chunks.append(ChunkData(
            chunk_id=abs(hash(p)),
            path=p,
            file_hash="hh",
            language="python",
            position=0,
            content="x",
            content_hash="cc",
            start_line=0,
            end_line=0,
            vector=[0.1] * 384,
            created_at=int(time.time() * 1_000_000),
        ))
    await storage.write_chunks(chunks)


# ---------------------------------------------------------------------------
# remove_stale_chunks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_stale_no_stale(storage):
    await _seed_chunks(storage, ["/tmp/a.py", "/tmp/b.py"])
    removed = await remove_stale_chunks(storage, current_paths={"/tmp/a.py", "/tmp/b.py"})
    assert removed == 0
    assert await storage.count() == 2


@pytest.mark.asyncio
async def test_remove_stale_some_stale(storage):
    await _seed_chunks(storage, ["/tmp/a.py", "/tmp/b.py", "/tmp/c.py"])
    removed = await remove_stale_chunks(storage, current_paths={"/tmp/a.py"})
    assert removed == 2  # b.py and c.py are stale
    assert await storage.count() == 1


@pytest.mark.asyncio
async def test_remove_stale_all_stale(storage):
    await _seed_chunks(storage, ["/tmp/a.py"])
    removed = await remove_stale_chunks(storage, current_paths=set())
    assert removed == 1
    assert await storage.count() == 0


@pytest.mark.asyncio
async def test_remove_stale_empty_storage(storage):
    removed = await remove_stale_chunks(storage, current_paths={"/tmp/anything.py"})
    assert removed == 0


# ---------------------------------------------------------------------------
# remove_chunks_for_paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_chunks_for_paths(storage):
    await _seed_chunks(storage, ["/tmp/a.py", "/tmp/b.py", "/tmp/c.py"])
    await remove_chunks_for_paths(storage, ["/tmp/b.py"])
    assert await storage.count() == 2

    hashes = await storage.get_file_hashes()
    assert "/tmp/b.py" not in hashes
    assert "/tmp/a.py" in hashes


@pytest.mark.asyncio
async def test_remove_chunks_empty_list(storage):
    await _seed_chunks(storage, ["/tmp/a.py"])
    await remove_chunks_for_paths(storage, [])
    assert await storage.count() == 1
