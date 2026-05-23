"""Tests for opencode_search.watcher — WatcherManager lifecycle and debounce.

These tests exercise the real watchdog Observer against tmp_path filesystems.
The Observer thread dispatches events back into the test's asyncio loop, so
we keep timing assertions loose (≥ DEBOUNCE_DELAY_MS, ≤ several seconds).
"""
# ruff: noqa: E402
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("watchdog")

from opencode_search.watcher import WatcherManager, watcher_manager

pytestmark = [pytest.mark.integration, pytest.mark.runtime_deps]


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
    except TimeoutError:
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
    except TimeoutError:
        pytest.fail("watcher never fired on_change for deletion")

    assert any("doomed.py" in p for p in received[0][1])


@pytest.mark.asyncio
async def test_watcher_dispatches_dotenv_event(fresh_manager, tmp_path, monkeypatch):
    """Indexable dotfiles such as `.env` must not be dropped by watcher filters."""
    monkeypatch.setattr("opencode_search.watcher.DEBOUNCE_DELAY_MS", 100)
    monkeypatch.setattr("opencode_search.watcher.MIN_FLUSH_INTERVAL_S", 0.1)

    env_file = tmp_path / ".env"
    env_file.write_text("TOKEN=alpha\n")

    received: list[tuple[list[Path], list[str]]] = []
    event = asyncio.Event()

    async def cb(modified, deleted):
        received.append((modified, deleted))
        event.set()

    await fresh_manager.start(tmp_path, on_change=cb)
    await asyncio.sleep(0.2)

    env_file.write_text("TOKEN=beta\n")

    try:
        await asyncio.wait_for(event.wait(), timeout=5.0)
    except TimeoutError:
        pytest.fail("watcher never fired on_change for .env modification")

    modified, deleted = received[0]
    assert deleted == []
    assert any(path.name == ".env" for path in modified)


def test_should_ignore_event_path_allows_project_symlink_to_external_target(tmp_path):
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    external_file = external_dir / "real.py"
    external_file.write_text("x = 1\n")

    project_root = tmp_path / "repo"
    project_root.mkdir()
    symlink_path = project_root / "link.py"
    symlink_path.symlink_to(external_file)

    assert WatcherManager._should_ignore_event_path(project_root, str(symlink_path)) is False


@pytest.mark.asyncio
async def test_watcher_ignores_internal_opencode_churn(fresh_manager, tmp_path, monkeypatch):
    """Internal `.opencode` activity must not starve real source-file flushes."""
    monkeypatch.setattr("opencode_search.watcher.DEBOUNCE_DELAY_MS", 200)
    monkeypatch.setattr("opencode_search.watcher.MIN_FLUSH_INTERVAL_S", 0.0)

    source_file = tmp_path / "app.py"
    internal_dir = tmp_path / ".opencode"
    internal_dir.mkdir()
    internal_file = internal_dir / "touch.log"

    received: list[tuple[list[Path], list[str]]] = []
    event = asyncio.Event()

    async def cb(modified, deleted):
        received.append((modified, deleted))
        event.set()

    await fresh_manager.start(tmp_path, on_change=cb)
    await asyncio.sleep(0.2)

    async def churn_internal_state() -> None:
        for i in range(30):
            internal_file.write_text(f"{i}\n")
            await asyncio.sleep(0.05)

    churn_task = asyncio.create_task(churn_internal_state())
    source_file.write_text("tracked = 1\n")

    try:
        await asyncio.wait_for(event.wait(), timeout=1.0)
    except TimeoutError:
        pytest.fail("watcher starved while `.opencode` churned")
    finally:
        await churn_task

    modified, deleted = received[0]
    assert deleted == []
    assert any(path.name == "app.py" for path in modified)
    assert not any(".opencode" in path.parts for path in modified)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


def test_watcher_manager_singleton_exists():
    assert watcher_manager is not None
    assert isinstance(watcher_manager, WatcherManager)


# ---------------------------------------------------------------------------
# Symlink support
# ---------------------------------------------------------------------------


def test_build_symlink_map_finds_symlinked_dirs(tmp_path):
    """_build_symlink_map returns real→symlink entries for directory symlinks."""
    from opencode_search.watcher import _build_symlink_map

    external = tmp_path / "external-repo"
    external.mkdir()
    (external / "main.go").write_text("package main\n")

    project = tmp_path / "project"
    project.mkdir()
    (project / "services").symlink_to(external, target_is_directory=True)

    smap = _build_symlink_map(str(project))

    assert str(external.resolve()) in smap
    assert smap[str(external.resolve())] == str(project / "services")


def test_build_symlink_map_skips_internal_symlinks(tmp_path):
    """Symlinks pointing inside the project root must not appear in the map."""
    from opencode_search.watcher import _build_symlink_map

    project = tmp_path / "project"
    project.mkdir()
    real_sub = project / "real"
    real_sub.mkdir()
    (project / "link").symlink_to(real_sub, target_is_directory=True)

    smap = _build_symlink_map(str(project))

    assert smap == {}


@pytest.mark.asyncio
async def test_watcher_dispatches_event_from_symlinked_directory(
    fresh_manager, tmp_path, monkeypatch
):
    """Changes inside a symlinked directory must fire on_change with the
    translated (symlink) path, not the resolved real path."""
    monkeypatch.setattr("opencode_search.watcher.DEBOUNCE_DELAY_MS", 100)
    monkeypatch.setattr("opencode_search.watcher.MIN_FLUSH_INTERVAL_S", 0.1)

    external = tmp_path / "external-service"
    external.mkdir()

    project = tmp_path / "monorepo"
    project.mkdir()
    symlink_dir = project / "services"
    symlink_dir.symlink_to(external, target_is_directory=True)

    received: list[tuple[list[Path], list[str]]] = []
    event = asyncio.Event()

    async def cb(modified, deleted):
        received.append((modified, deleted))
        event.set()

    await fresh_manager.start(project, on_change=cb)
    await asyncio.sleep(0.3)

    (external / "handler.go").write_text("package main\n")

    try:
        await asyncio.wait_for(event.wait(), timeout=8.0)
    except TimeoutError:
        pytest.fail("watcher never fired for change inside symlinked directory")

    assert len(received) >= 1
    modified, _ = received[0]
    # Path must be reported under the symlink (project root), not the real target
    assert any(str(project) in str(p) for p in modified), (
        f"expected path under {project}, got {[str(p) for p in modified]}"
    )
