"""Tests for opencode_search.compaction — gated and forced compaction."""
# ruff: noqa: E402
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

pytest.importorskip("lancedb")
pytest.importorskip("pyarrow")
import pytest_asyncio

from opencode_search.compaction import (
    COMPACTION_THRESHOLD_OPS,
    compact_if_needed,
    force_compact,
)
from opencode_search.storage import Storage

pytestmark = [pytest.mark.integration, pytest.mark.runtime_deps]


@pytest_asyncio.fixture
async def storage(tmp_path):
    s = Storage(db_path=str(tmp_path / "db"), dims=384)
    await s.open()
    yield s
    await s.close()


# ---------------------------------------------------------------------------
# compact_if_needed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_if_needed_below_threshold(storage):
    result = await compact_if_needed(storage, ops_since_last=COMPACTION_THRESHOLD_OPS - 1)
    assert result is False


@pytest.mark.asyncio
async def test_compact_if_needed_at_threshold(storage):
    result = await compact_if_needed(storage, ops_since_last=COMPACTION_THRESHOLD_OPS)
    assert result is True


@pytest.mark.asyncio
async def test_compact_if_needed_above_threshold(storage):
    result = await compact_if_needed(storage, ops_since_last=COMPACTION_THRESHOLD_OPS * 2)
    assert result is True


@pytest.mark.asyncio
async def test_compact_if_needed_uses_storage_compact():
    """compact_if_needed must call storage.compact() (not storage.table.compact_files)."""
    mock_storage = AsyncMock()
    mock_storage.compact = AsyncMock()

    result = await compact_if_needed(mock_storage, ops_since_last=COMPACTION_THRESHOLD_OPS)

    assert result is True
    mock_storage.compact.assert_awaited_once()


@pytest.mark.asyncio
async def test_compact_if_needed_handles_compact_exception():
    mock_storage = AsyncMock()
    mock_storage.compact = AsyncMock(side_effect=RuntimeError("LanceDB error"))

    result = await compact_if_needed(mock_storage, ops_since_last=COMPACTION_THRESHOLD_OPS)

    assert result is False


# ---------------------------------------------------------------------------
# force_compact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_compact_success(storage):
    result = await force_compact(storage)
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_force_compact_handles_exception():
    mock_storage = AsyncMock()
    mock_storage.compact = AsyncMock(side_effect=RuntimeError("oops"))

    result = await force_compact(mock_storage)

    assert result["status"] == "error"
    assert "oops" in result["error"]


@pytest.mark.asyncio
async def test_force_compact_calls_storage_compact():
    mock_storage = AsyncMock()
    mock_storage.compact = AsyncMock()

    await force_compact(mock_storage)

    mock_storage.compact.assert_awaited_once()
