"""Tests for opencode_search.watcher — WatcherManager lifecycle and debounce.

These tests exercise the real watchdog Observer against tmp_path filesystems.
The Observer thread dispatches events back into the test's asyncio loop, so
we keep timing assertions loose (≥ DEBOUNCE_DELAY_MS, ≤ several seconds).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from opencode_search.watcher import WatcherManager, watcher_manager


@pytest.fixture
async def fresh_manager():
    m = WatcherManager()
    yield m
    await m.stop_all()


# ---------------------------------------------------------------------------
# Lifecycle: is_active / start / stop / list_active
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watcher_is_active_initially_false(fresh_manager, tmp_path):
    assert fresh_manager.is_active(str(tmp_path)) is False


@pytest.mark.asyncio
async def test_watcher_start_returns_true(fresh_manager, tmp_path):
    async def noop_cb(modified, deleted):
        pass

    ok = await fresh_manager.start(tmp_path, on_change=noop_cb)
    assert ok is True
    assert fresh_manager.is_active(str(tmp_path.resolve())) is True


@pytest.mark.asyncio
async def test_watcher_start_idempotent(fresh_manager, tmp_path):
    """Calling start() twice on the same root is a no-op."""
    async def noop_cb(modified, deleted):
        pass

    ok1 = await fresh_manager.start(tmp_path, on_change=noop_cb)
    ok2 = await fresh_manager.start(tmp_path, on_change=noop_cb)
    assert ok1 is True
    assert ok2 is True
    # Should be present in list_active exactly once
    actives = fresh_manager.list_active()
    assert actives.count(str(tmp_path.resolve())) == 1


@pytest.mark.asyncio
async def test_watcher_stop(fresh_manager, tmp_path):
    async def noop_cb(modified, deleted):
        pass

    await fresh_manager.start(tmp_path, on_change=noop_cb)
    assert fresh_manager.is_active(str(tmp_path.resolve())) is True

    await fresh_manager.stop(tmp_path)
    assert fresh_manager.is_active(str(tmp_path.resolve())) is False


@pytest.mark.asyncio
async def test_watcher_stop_not_active(fresh_manager, tmp_path):
    """Stopping a non-active watcher is a no-op (doesn't raise)."""
    await fresh_manager.stop(tmp_path)
    assert fresh_manager.is_active(str(tmp_path.resolve())) is False


@pytest.mark.asyncio
async def test_watcher_stop_all(fresh_manager, tmp_path):
    """stop_all() stops every active watcher."""
    p1 = tmp_path / "a"
    p2 = tmp_path / "b"
    p1.mkdir()
    p2.mkdir()

    async def noop_cb(modified, deleted):
        pass

    await fresh_manager.start(p1, on_change=noop_cb)
    await fresh_manager.start(p2, on_change=noop_cb)
    assert len(fresh_manager.list_active()) == 2

    await fresh_manager.stop_all()
    assert fresh_manager.list_active() == []


@pytest.mark.asyncio
async def test_watcher_list_active_isolation(fresh_manager, tmp_path):
    """list_active() returns only active roots; stopping removes from the list."""
    p1 = tmp_path / "a"
    p2 = tmp_path / "b"
    p1.mkdir()
    p2.mkdir()

    async def noop_cb(modified, deleted):
        pass

    await fresh_manager.start(p1, on_change=noop_cb)
    await fresh_manager.start(p2, on_change=noop_cb)

    actives_before = set(fresh_manager.list_active())
    assert str(p1.resolve()) in actives_before
    assert str(p2.resolve()) in actives_before

    await fresh_manager.stop(p1)
    actives_after = set(fresh_manager.list_active())
    assert str(p1.resolve()) not in actives_after
    assert str(p2.resolve()) in actives_after


# ---------------------------------------------------------------------------
# Live event dispatch (uses real watchdog Observer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watcher_dispatches_modified_event(fresh_manager, tmp_path, monkeypatch):
    """Modifying a file under the watched root triggers on_change."""
    # Shrink debounce so the test runs quickly
    monkeypatch.setattr("opencode_search.watcher.DEBOUNCE_DELAY_MS", 100)
    monkeypatch.setattr("opencode_search.watcher.MIN_FLUSH_INTERVAL_S", 0.1)

    received: list[tuple[list[Path], list[str]]] = []
    event = asyncio.Event()

    async def cb(modified, deleted):
        received.append((modified, deleted))
        event.set()

    await fresh_manager.start(tmp_path, on_change=cb)
    # Give the observer thread a moment to bind
    await asyncio.sleep(0.2)

    # Trigger an event
    (tmp_path / "newfile.py").write_text("x = 1\n")

    try:
        await asyncio.wait_for(event.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        pytest.fail("watcher never fired on_change after file create")

    assert len(received) >= 1
    modified, deleted = received[0]
    assert any("newfile.py" in str(p) for p in modified)


@pytest.mark.asyncio
async def test_watcher_dispatches_deleted_event(fresh_manager, tmp_path, monkeypatch):
    """Deleting a file under the watched root triggers on_change with deleted set."""
    monkeypatch.setattr("opencode_search.watcher.DEBOUNCE_DELAY_MS", 100)
    monkeypatch.setattr("opencode_search.watcher.MIN_FLUSH_INTERVAL_S", 0.1)

    f = tmp_path / "doomed.py"
    f.write_text("x = 1\n")

    received: list[tuple[list[Path], list[str]]] = []
    event = asyncio.Event()

    async def cb(modified, deleted):
        if deleted:
            received.append((modified, deleted))
            event.set()

    await fresh_manager.start(tmp_path, on_change=cb)
    await asyncio.sleep(0.2)

    f.unlink()

    try:
        await asyncio.wait_for(event.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        pytest.fail("watcher never fired on_change for deletion")

    assert any("doomed.py" in p for p in received[0][1])


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


def test_watcher_manager_singleton_exists():
    assert watcher_manager is not None
    assert isinstance(watcher_manager, WatcherManager)
