"""Tests for opencode_search.handlers — MCP tool handler logic.

All tests mock GPU-dependent calls (embed/rerank/indexer).
No GPU required unless @pytest.mark.gpu.
"""
# ruff: noqa: N806
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opencode_search.config import ProjectEntry, get_tier_dims

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeIndexResult:
    files_indexed: int = 3
    files_unchanged: int = 1
    files_removed: int = 0
    chunks_total: int = 12
    errors: int = 0
    elapsed_s: float = 0.5


def _make_entry(path: str, tier: str = "balanced") -> ProjectEntry:
    dims = get_tier_dims(tier)
    return ProjectEntry(
        path=path,
        db_path=f"{path}/.opencode/index_{tier}",
        tier=tier,
        dims=dims,
    )


# ---------------------------------------------------------------------------
# handle_index_project
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_index_project_invalid_tier(tmp_path):
    from opencode_search.handlers import handle_index_project
    result = await handle_index_project(path=str(tmp_path), tier="nonexistent")
    assert "error" in result
    assert "tier" in result["error"].lower()


@pytest.mark.asyncio
async def test_handle_index_project_missing_dir():
    from opencode_search.handlers import handle_index_project
    result = await handle_index_project(path="/nonexistent/path/xyz", tier="balanced")
    assert "error" in result
    assert "not found" in result["error"].lower() or "directory" in result["error"].lower()


@pytest.mark.asyncio
async def test_handle_index_project_success(tmp_path):
    from opencode_search.handlers import handle_index_project

    with patch("opencode_search.handlers._index_project", AsyncMock(return_value=_FakeIndexResult())), \
         patch("opencode_search.handlers.load_registry", return_value={}), \
         patch("opencode_search.handlers.save_registry"), \
         patch("opencode_search.handlers.clear_search_cache"), \
         patch("opencode_search.handlers.Storage") as MockStorage, \
         patch("opencode_search.handlers.watcher_manager") as MockWatcher:
        mock_st = MagicMock()
        mock_st.open = AsyncMock()
        mock_st.close = AsyncMock()
        MockStorage.return_value = mock_st
        MockWatcher.is_active.return_value = False

        result = await handle_index_project(path=str(tmp_path), tier="balanced")

    assert result.get("status") == "ok"
    assert result["files_indexed"] == 3
    assert result["chunks_total"] == 12
    assert result["errors"] == 0


@pytest.mark.asyncio
async def test_handle_index_project_no_duplicate_run(tmp_path):
    from opencode_search.handlers import _indexing_status, handle_index_project

    path_str = str(tmp_path)
    _indexing_status[path_str] = {"running": True}

    result = await handle_index_project(path=path_str, tier="balanced")
    assert result.get("status") == "already_indexing"

    del _indexing_status[path_str]


@pytest.mark.asyncio
async def test_handle_index_project_clears_running_on_exception(tmp_path):
    from opencode_search.handlers import _indexing_status, handle_index_project

    with patch("opencode_search.handlers.Storage") as MockStorage:
        mock_st = MagicMock()
        mock_st.open = AsyncMock(side_effect=RuntimeError("db failed"))
        mock_st.close = AsyncMock()
        MockStorage.return_value = mock_st

        result = await handle_index_project(path=str(tmp_path), tier="balanced")

    path_str = str(tmp_path.resolve())
    assert result["status"] == "error"
    assert _indexing_status[path_str]["running"] is False


@pytest.mark.asyncio
async def test_handle_index_project_preserves_existing_watch_on_plain_reindex(tmp_path):
    from opencode_search.handlers import handle_index_project

    path_str = str(tmp_path.resolve())
    existing = _make_entry(path_str)
    existing.watch = True

    saved_registry: dict[str, ProjectEntry] = {}

    def _capture_save(registry):
        saved_registry.clear()
        saved_registry.update(registry)

    with patch("opencode_search.handlers._index_project", AsyncMock(return_value=_FakeIndexResult())), \
         patch("opencode_search.handlers.load_registry", return_value={path_str: existing}), \
         patch("opencode_search.handlers.save_registry", side_effect=_capture_save), \
         patch("opencode_search.handlers.clear_search_cache"), \
         patch("opencode_search.handlers.Storage") as MockStorage, \
         patch("opencode_search.handlers.watcher_manager") as MockWatcher:
        mock_st = MagicMock()
        mock_st.open = AsyncMock()
        mock_st.close = AsyncMock()
        MockStorage.return_value = mock_st
        MockWatcher.is_active.return_value = True

        result = await handle_index_project(path=path_str, tier="balanced", watch=False)

    assert result["status"] == "ok"
    assert saved_registry[path_str].watch is True


# ---------------------------------------------------------------------------
# handle_search_code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_search_code_empty_query():
    from opencode_search.handlers import handle_search_code
    result = await handle_search_code(query="")
    assert "error" in result


@pytest.mark.asyncio
async def test_handle_search_code_no_registry():
    from opencode_search.handlers import handle_search_code

    with patch("opencode_search.handlers.load_registry", return_value={}):
        result = await handle_search_code(query="find something")

    assert "note" in result or "results" in result


@pytest.mark.asyncio
async def test_handle_search_code_with_results():
    from opencode_search.handlers import handle_search_code
    from opencode_search.search import SearchResult

    fake_results = [
        SearchResult(
            path="/tmp/foo.py",
            content="def foo(): pass",
            language="python",
            start_line=1,
            end_line=5,
            score=0.95,
            project_path="/tmp",
        )
    ]

    with patch("opencode_search.handlers.load_registry",
               return_value={"/tmp": _make_entry("/tmp")}), \
         patch("opencode_search.handlers.search", AsyncMock(return_value=fake_results)):
        result = await handle_search_code(query="find function")

    assert "results" in result
    assert len(result["results"]) == 1
    assert result["results"][0]["path"] == "/tmp/foo.py"
    assert result["results"][0]["score"] == 0.95


@pytest.mark.asyncio
async def test_handle_search_code_filters_by_project_paths():
    from opencode_search.handlers import handle_search_code

    registry = {
        "/tmp/a": _make_entry("/tmp/a"),
        "/tmp/b": _make_entry("/tmp/b"),
    }

    searched_projects = []

    async def capture_search(query, *, projects, **kwargs):
        searched_projects.extend(projects)
        return []

    with patch("opencode_search.handlers.load_registry", return_value=registry), \
         patch("opencode_search.handlers.search", side_effect=capture_search):
        await handle_search_code(query="test", project_paths=["/tmp/a"])

    assert len(searched_projects) == 1
    assert searched_projects[0].path == "/tmp/a"


@pytest.mark.asyncio
async def test_handle_search_code_missing_project_paths():
    from opencode_search.handlers import handle_search_code

    with patch("opencode_search.handlers.load_registry", return_value={"/tmp/a": _make_entry("/tmp/a")}):
        result = await handle_search_code(query="test", project_paths=["/tmp/nonexistent"])

    assert "error" in result


@pytest.mark.asyncio
async def test_handle_search_code_returns_error_for_mixed_tiers():
    from opencode_search.handlers import handle_search_code

    registry = {
        "/tmp/a": _make_entry("/tmp/a", tier="budget"),
        "/tmp/b": _make_entry("/tmp/b", tier="balanced"),
    }

    with patch("opencode_search.handlers.load_registry", return_value=registry):
        result = await handle_search_code(query="test")

    assert "error" in result
    assert "Mixed-tier" in result["error"]


# ---------------------------------------------------------------------------
# handle_project_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_project_status_not_indexed():
    from opencode_search.handlers import handle_project_status

    with patch("opencode_search.handlers.load_registry", return_value={}):
        result = await handle_project_status(path="/tmp/unknown")

    assert result["indexed"] is False


@pytest.mark.asyncio
async def test_handle_project_status_indexed():
    from opencode_search.handlers import handle_project_status

    entry = _make_entry("/tmp/proj")
    registry = {"/tmp/proj": entry}

    mock_storage = MagicMock()
    mock_storage.open = AsyncMock()
    mock_storage.close = AsyncMock()
    mock_storage.count = AsyncMock(return_value=42)

    with patch("opencode_search.handlers.load_registry", return_value=registry), \
         patch("opencode_search.handlers.Storage", return_value=mock_storage), \
         patch("opencode_search.handlers.watcher_manager") as MockWatcher:
        MockWatcher.is_active.return_value = False

        result = await handle_project_status(path="/tmp/proj")

    assert result["indexed"] is True
    assert result["tier"] == "balanced"
    assert result["chunks"] == 42


# ---------------------------------------------------------------------------
# handle_list_indexed_projects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_list_indexed_projects_empty():
    from opencode_search.handlers import handle_list_indexed_projects

    with patch("opencode_search.handlers.load_registry", return_value={}), \
         patch("opencode_search.handlers.watcher_manager") as MockWatcher:
        MockWatcher.list_active.return_value = []
        result = await handle_list_indexed_projects()

    assert result["projects"] == []


@pytest.mark.asyncio
async def test_handle_list_indexed_projects_with_entries():
    from opencode_search.handlers import handle_list_indexed_projects

    registry = {
        "/tmp/a": _make_entry("/tmp/a", tier="budget"),
        "/tmp/b": _make_entry("/tmp/b", tier="premium"),
    }

    with patch("opencode_search.handlers.load_registry", return_value=registry), \
         patch("opencode_search.handlers.watcher_manager") as MockWatcher:
        MockWatcher.list_active.return_value = ["/tmp/a"]
        result = await handle_list_indexed_projects()

    assert len(result["projects"]) == 2
    paths = {p["path"] for p in result["projects"]}
    assert paths == {"/tmp/a", "/tmp/b"}
    watching = {p["path"]: p["watching"] for p in result["projects"]}
    assert watching["/tmp/a"] is True
    assert watching["/tmp/b"] is False


# ---------------------------------------------------------------------------
# auto watch lifecycle helpers
# ---------------------------------------------------------------------------


def test_resolve_indexed_project_path_prefers_nearest_ancestor():
    from opencode_search.handlers import resolve_indexed_project_path

    registry = {
        "/tmp/work": _make_entry("/tmp/work"),
        "/tmp/work/repo": _make_entry("/tmp/work/repo"),
    }

    with patch("opencode_search.handlers.load_registry", return_value=registry):
        resolved = resolve_indexed_project_path("/tmp/work/repo/src/module.py")

    assert resolved == "/tmp/work/repo"


@pytest.mark.asyncio
async def test_handle_ensure_project_watching_starts_for_indexed_ancestor():
    from opencode_search.handlers import handle_ensure_project_watching

    entry = _make_entry("/tmp/work/repo")

    started: dict[str, object] = {}

    async def _mock_start(root, *, on_change):
        started["root"] = root
        started["callback"] = on_change
        return True

    with patch("opencode_search.handlers.load_registry", return_value={entry.path: entry}), \
         patch("opencode_search.handlers.watcher_manager") as MockWatcher:
        MockWatcher.is_active.return_value = False
        MockWatcher.start = AsyncMock(side_effect=_mock_start)

        result = await handle_ensure_project_watching("/tmp/work/repo/src/module.py")

    assert result["status"] == "ok"
    assert result["path"] == entry.path
    assert started["root"] == entry.path
    assert started["callback"] is not None


@pytest.mark.asyncio
async def test_handle_release_project_watch_keeps_persisted_watch():
    from opencode_search.handlers import handle_release_project_watch

    entry = _make_entry("/tmp/proj")
    entry.watch = True

    with patch("opencode_search.handlers.load_registry", return_value={entry.path: entry}), \
         patch("opencode_search.handlers.watcher_manager") as MockWatcher:
        MockWatcher.is_active.return_value = True
        MockWatcher.stop = AsyncMock()

        result = await handle_release_project_watch("/tmp/proj")

    assert result["status"] == "kept_persisted"
    MockWatcher.stop.assert_not_called()


@pytest.mark.asyncio
async def test_handle_release_project_watch_stops_non_persisted_watch():
    from opencode_search.handlers import handle_release_project_watch

    entry = _make_entry("/tmp/proj")
    entry.watch = False

    with patch("opencode_search.handlers.load_registry", return_value={entry.path: entry}), \
         patch("opencode_search.handlers.watcher_manager") as MockWatcher:
        MockWatcher.is_active.return_value = True
        MockWatcher.stop = AsyncMock()

        result = await handle_release_project_watch("/tmp/proj/subdir")

    assert result["status"] == "stopped"
    MockWatcher.stop.assert_called_once_with("/tmp/proj")


# ---------------------------------------------------------------------------
# handle_stop_watching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_stop_watching_not_active():
    from opencode_search.handlers import handle_stop_watching

    with patch("opencode_search.handlers.watcher_manager") as MockWatcher, \
         patch("opencode_search.handlers.load_registry", return_value={}), \
         patch("opencode_search.handlers.save_registry"):
        MockWatcher.is_active.return_value = False
        MockWatcher.stop = AsyncMock()

        result = await handle_stop_watching(path="/tmp/proj")

    assert result["was_watching"] is False
    assert result["status"] == "stopped"


@pytest.mark.asyncio
async def test_handle_stop_watching_was_active():
    from opencode_search.handlers import handle_stop_watching

    entry = _make_entry("/tmp/proj")

    with patch("opencode_search.handlers.watcher_manager") as MockWatcher, \
         patch("opencode_search.handlers.load_registry", return_value={"/tmp/proj": entry}), \
         patch("opencode_search.handlers.save_registry"):
        MockWatcher.is_active.return_value = True
        MockWatcher.stop = AsyncMock()

        result = await handle_stop_watching(path="/tmp/proj")

    assert result["was_watching"] is True
    MockWatcher.stop.assert_called_once()
