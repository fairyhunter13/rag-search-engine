"""Tests for opencode_search.handlers — MCP tool handler logic.

All tests mock GPU-dependent calls (embed/rerank/indexer).
No GPU required unless @pytest.mark.gpu.

Patch targets use the submodule where the name is bound, not the package root:
  - indexing logic  → opencode_search.handlers._index.*
  - query logic     → opencode_search.handlers._query.*
  - watch logic     → opencode_search.handlers._watch.*
  - shared utils    → opencode_search.handlers._common.*
"""
# ruff: noqa: N806
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opencode_search.config import DEFAULT_DIMS, ProjectEntry, get_project_db_path

_IDX = "opencode_search.handlers._index"
_QRY = "opencode_search.handlers._query"
_WCH = "opencode_search.handlers._watch"
_CMN = "opencode_search.handlers._common"


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


def _make_entry(path: str) -> ProjectEntry:
    return ProjectEntry(
        path=path,
        db_path=get_project_db_path(path),
        dims=DEFAULT_DIMS,
    )


# ---------------------------------------------------------------------------
# handle_index_project
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_index_project_missing_dir():
    from opencode_search.handlers import handle_index_project
    result = await handle_index_project(path="/nonexistent/path/xyz")
    assert "error" in result
    assert "not found" in result["error"].lower() or "directory" in result["error"].lower()


@pytest.mark.asyncio
async def test_handle_index_project_success(tmp_path):
    import asyncio
    from opencode_search.handlers import _indexing_status, handle_index_project
    expected_db_path = get_project_db_path(tmp_path)

    with patch(f"{_IDX}._index_project", AsyncMock(return_value=_FakeIndexResult())), \
         patch(f"{_IDX}.load_registry", return_value={}), \
         patch(f"{_IDX}.save_registry"), \
         patch(f"{_IDX}.clear_search_cache"), \
         patch(f"{_IDX}.Storage") as MockStorage, \
         patch(f"{_IDX}.watcher_manager") as MockWatcher:
        mock_st = MagicMock()
        mock_st.open = AsyncMock()
        mock_st.close = AsyncMock()
        mock_st.compact_before_index = AsyncMock()
        MockStorage.return_value = mock_st
        MockWatcher.is_active.return_value = False

        result = await handle_index_project(path=str(tmp_path))
        assert result.get("status") == "indexing"
        assert "started_at" in result

        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        MockStorage.assert_called_once_with(db_path=expected_db_path, dims=DEFAULT_DIMS)

    path_str = str(tmp_path.resolve())
    assert _indexing_status[path_str]["running"] is False
    assert _indexing_status[path_str].get("status") == "ok"
    assert _indexing_status[path_str]["files_indexed"] == 3
    assert _indexing_status[path_str]["chunks_total"] == 12
    assert _indexing_status[path_str]["errors"] == 0


@pytest.mark.asyncio
async def test_handle_index_project_no_duplicate_run(tmp_path):
    from opencode_search.handlers import _indexing_status, handle_index_project

    path_str = str(tmp_path)
    _indexing_status[path_str] = {"running": True}

    result = await handle_index_project(path=path_str)
    assert result.get("status") == "already_indexing"

    del _indexing_status[path_str]


@pytest.mark.asyncio
async def test_handle_index_project_clears_running_on_exception(tmp_path):
    import asyncio
    from opencode_search.handlers import _indexing_status, handle_index_project

    with patch(f"{_IDX}.Storage") as MockStorage:
        mock_st = MagicMock()
        mock_st.open = AsyncMock(side_effect=RuntimeError("db failed"))
        mock_st.close = AsyncMock()
        MockStorage.return_value = mock_st

        result = await handle_index_project(path=str(tmp_path))
        assert result.get("status") == "indexing"

        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    path_str = str(tmp_path.resolve())
    assert _indexing_status[path_str]["running"] is False
    assert _indexing_status[path_str].get("status") == "error"


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

    with patch(f"{_IDX}._index_project", AsyncMock(return_value=_FakeIndexResult())), \
         patch(f"{_IDX}.load_registry", return_value={path_str: existing}), \
         patch(f"{_IDX}.save_registry", side_effect=_capture_save), \
         patch(f"{_IDX}.clear_search_cache"), \
         patch(f"{_IDX}.Storage") as MockStorage, \
         patch(f"{_IDX}.watcher_manager") as MockWatcher:
        mock_st = MagicMock()
        mock_st.open = AsyncMock()
        mock_st.close = AsyncMock()
        mock_st.compact_before_index = AsyncMock()
        MockStorage.return_value = mock_st
        MockWatcher.is_active.return_value = True

        result = await handle_index_project(path=path_str, watch=False)
        assert result.get("status") == "indexing"

        import asyncio
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    assert saved_registry[path_str].watch is True


@pytest.mark.asyncio
async def test_handle_project_status_skips_missing_db_without_recreating(tmp_path):
    from opencode_search.handlers import handle_project_status

    project_root = tmp_path / "project"
    project_root.mkdir()
    entry = ProjectEntry(
        path=str(project_root),
        db_path=str(tmp_path / "central-index" / "index"),
        dims=DEFAULT_DIMS,
    )

    with patch(f"{_QRY}.load_registry", return_value={str(project_root): entry}), \
         patch(f"{_QRY}.watcher_manager") as MockWatcher, \
         patch(f"{_QRY}.Storage") as MockStorage:
        MockWatcher.is_active.return_value = False
        result = await handle_project_status(path=str(project_root))

    MockStorage.assert_not_called()
    assert result["indexed"] is True
    assert result["chunks"] is None


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

    with patch(f"{_QRY}.load_registry", return_value={}):
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

    with patch(f"{_QRY}.load_registry", return_value={"/tmp": _make_entry("/tmp")}), \
         patch(f"{_QRY}.search", AsyncMock(return_value=fake_results)):
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

    with patch(f"{_QRY}.load_registry", return_value=registry), \
         patch(f"{_QRY}.search", side_effect=capture_search):
        await handle_search_code(query="test", project_paths=["/tmp/a"])

    assert len(searched_projects) == 1
    assert searched_projects[0].path == "/tmp/a"


@pytest.mark.asyncio
async def test_handle_search_code_missing_project_paths():
    from opencode_search.handlers import handle_search_code

    with patch(f"{_QRY}.load_registry", return_value={"/tmp/a": _make_entry("/tmp/a")}):
        result = await handle_search_code(query="test", project_paths=["/tmp/nonexistent"])

    assert "error" in result


@pytest.mark.asyncio
async def test_handle_search_code_no_tier_validation():
    """search_code no longer raises mixed-tier errors — all projects use same model."""
    from opencode_search.handlers import handle_search_code

    registry = {
        "/tmp/a": _make_entry("/tmp/a"),
        "/tmp/b": _make_entry("/tmp/b"),
    }

    with patch(f"{_QRY}.load_registry", return_value=registry), \
         patch(f"{_QRY}.search", AsyncMock(return_value=[])):
        result = await handle_search_code(query="test")

    assert "error" not in result or "Mixed-tier" not in result.get("error", "")


# ---------------------------------------------------------------------------
# handle_project_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_project_status_not_indexed():
    from opencode_search.handlers import handle_project_status

    with patch(f"{_QRY}.load_registry", return_value={}):
        result = await handle_project_status(path="/tmp/unknown")

    assert result["indexed"] is False


@pytest.mark.asyncio
async def test_handle_project_status_indexed(tmp_path):
    from opencode_search.handlers import handle_project_status

    project_root = tmp_path / "proj"
    project_root.mkdir()
    entry = _make_entry(str(project_root))
    Path(entry.db_path).mkdir(parents=True)
    registry = {str(project_root): entry}

    mock_storage = MagicMock()
    mock_storage.open = AsyncMock()
    mock_storage.close = AsyncMock()
    mock_storage.count = AsyncMock(return_value=42)

    with patch(f"{_QRY}.load_registry", return_value=registry), \
         patch(f"{_QRY}.Storage", return_value=mock_storage), \
         patch(f"{_QRY}.watcher_manager") as MockWatcher:
        MockWatcher.is_active.return_value = False

        result = await handle_project_status(path=str(project_root))

    assert result["indexed"] is True
    assert "tier" not in result, "tier key should not be in project_status result"
    assert result["chunks"] == 42


# ---------------------------------------------------------------------------
# handle_list_indexed_projects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_list_indexed_projects_empty():
    from opencode_search.handlers import handle_list_indexed_projects

    with patch(f"{_QRY}.load_registry", return_value={}), \
         patch(f"{_QRY}.watcher_manager") as MockWatcher:
        MockWatcher.list_active.return_value = []
        result = await handle_list_indexed_projects()

    assert result["projects"] == []


@pytest.mark.asyncio
async def test_handle_list_indexed_projects_with_entries():
    from opencode_search.handlers import handle_list_indexed_projects

    registry = {
        "/tmp/a": _make_entry("/tmp/a"),
        "/tmp/b": _make_entry("/tmp/b"),
    }

    with patch(f"{_QRY}.load_registry", return_value=registry), \
         patch(f"{_QRY}.watcher_manager") as MockWatcher:
        MockWatcher.list_active.return_value = ["/tmp/a"]
        result = await handle_list_indexed_projects()

    assert len(result["projects"]) == 2
    paths = {p["path"] for p in result["projects"]}
    assert paths == {"/tmp/a", "/tmp/b"}
    watching = {p["path"]: p["watching"] for p in result["projects"]}
    assert watching["/tmp/a"] is True
    assert watching["/tmp/b"] is False
    for p in result["projects"]:
        assert "tier" not in p, "tier key should not appear in list_indexed_projects"


# ---------------------------------------------------------------------------
# auto watch lifecycle helpers
# ---------------------------------------------------------------------------


def test_resolve_indexed_project_path_prefers_nearest_ancestor():
    from opencode_search.handlers import resolve_indexed_project_path

    registry = {
        "/tmp/work": _make_entry("/tmp/work"),
        "/tmp/work/repo": _make_entry("/tmp/work/repo"),
    }

    with patch(f"{_CMN}.load_registry", return_value=registry):
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

    with patch(f"{_WCH}.resolve_indexed_project_path", return_value=entry.path), \
         patch(f"{_WCH}.load_registry", return_value={entry.path: entry}), \
         patch(f"{_WCH}.watcher_manager") as MockWatcher:
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

    with patch(f"{_WCH}.resolve_indexed_project_path", return_value=entry.path), \
         patch(f"{_WCH}.load_registry", return_value={entry.path: entry}), \
         patch(f"{_WCH}.watcher_manager") as MockWatcher:
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

    with patch(f"{_WCH}.resolve_indexed_project_path", return_value=entry.path), \
         patch(f"{_WCH}.load_registry", return_value={entry.path: entry}), \
         patch(f"{_WCH}.watcher_manager") as MockWatcher:
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

    with patch(f"{_WCH}.watcher_manager") as MockWatcher, \
         patch(f"{_WCH}.load_registry", return_value={}), \
         patch(f"{_WCH}.save_registry"):
        MockWatcher.is_active.return_value = False
        MockWatcher.stop = AsyncMock()

        result = await handle_stop_watching(path="/tmp/proj")

    assert result["was_watching"] is False
    assert result["status"] == "stopped"


@pytest.mark.asyncio
async def test_handle_stop_watching_was_active():
    from opencode_search.handlers import handle_stop_watching

    entry = _make_entry("/tmp/proj")

    with patch(f"{_WCH}.watcher_manager") as MockWatcher, \
         patch(f"{_WCH}.load_registry", return_value={"/tmp/proj": entry}), \
         patch(f"{_WCH}.save_registry"):
        MockWatcher.is_active.return_value = True
        MockWatcher.stop = AsyncMock()

        result = await handle_stop_watching(path="/tmp/proj")

    assert result["was_watching"] is True
    MockWatcher.stop.assert_called_once()
