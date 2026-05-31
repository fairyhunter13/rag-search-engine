"""Tests for opencode_search.mcp — tool registration and server setup.

These tests verify that:
- FastMCP server imports cleanly
- All 7 v2 intent tools are registered
- Tool names are correct
- run_mcp_server / run_mcp_http_server enforce the GPU guard

No GPU required (GPU guard is patched per test where needed).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

# opencode_search.mcp unconditionally imports starlette; skip the whole module
# when running under system Python instead of the project .venv.
pytest.importorskip("starlette", reason="starlette not installed — run tests with .venv/bin/pytest")


def _import_mcp():
    """Import the mcp module, which triggers FastMCP server instantiation."""
    import importlib
    if "opencode_search.mcp" in __import__("sys").modules:
        return __import__("sys").modules["opencode_search.mcp"]
    return importlib.import_module("opencode_search.mcp")


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


# ---------------------------------------------------------------------------
# Import sanity
# ---------------------------------------------------------------------------


def test_mcp_imports():
    mod = _import_mcp()
    assert mod is not None


def test_mcp_server_instance():
    mod = _import_mcp()
    assert hasattr(mod, "mcp")
    assert mod.mcp is not None


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def test_mcp_has_search_tool():
    mod = _import_mcp()
    assert hasattr(mod, "search"), "v2 `search` tool must exist"


def test_mcp_has_ask_tool():
    mod = _import_mcp()
    assert hasattr(mod, "ask"), "v2 `ask` tool must exist"


def test_mcp_has_graph_tool():
    mod = _import_mcp()
    assert hasattr(mod, "graph"), "v2 `graph` tool must exist"


def test_mcp_has_overview_tool():
    mod = _import_mcp()
    assert hasattr(mod, "overview"), "v2 `overview` tool must exist"


def test_mcp_has_build_tool():
    mod = _import_mcp()
    assert hasattr(mod, "build"), "v2 `build` tool must exist"


def test_mcp_has_federation_tool():
    mod = _import_mcp()
    assert hasattr(mod, "federation"), "v2 `federation` tool must exist"


def test_mcp_has_manage_tool():
    mod = _import_mcp()
    assert hasattr(mod, "manage"), "v2 `manage` tool must exist"


def test_mcp_has_run_mcp_server():
    mod = _import_mcp()
    assert callable(mod.run_mcp_server)


# ---------------------------------------------------------------------------
# Tool callability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_index_callable():
    """build(action=index) tool should be callable and trigger handle_index_project."""
    mod = _import_mcp()
    with patch("opencode_search.mcp.handle_index_project",
               AsyncMock(return_value={"status": "indexing", "path": "/tmp/x",
                                       "started_at": "2026-01-01T00:00:00"})):
        result = await mod.build(project_path="/tmp/x", action="index")
    assert result is not None


@pytest.mark.asyncio
async def test_build_index_auto_starts_watch_for_matching_open_client():
    mod = _import_mcp()

    async def _fake_handle(*, path, watch, force, follow_symlinks, on_complete=None):
        ok_result = {"status": "ok", "path": "/tmp/proj", "watching": False}
        if on_complete:
            await on_complete(ok_result)
        return {"status": "indexing", "path": "/tmp/proj", "started_at": "2026-01-01T00:00:00"}

    with patch("opencode_search.mcp.handle_index_project", _fake_handle), \
         patch.object(mod.runtime_state, "bind_clients_to_project", return_value=1) as mock_bind, \
         patch("opencode_search.mcp.handle_ensure_project_watching",
               AsyncMock(return_value={"status": "ok"})) as mock_watch:
        result = await mod.build(project_path="/tmp/proj", action="index")

    assert result["status"] == "indexing"
    mock_bind.assert_called_once_with("/tmp/proj")
    mock_watch.assert_awaited_once_with("/tmp/proj", persist=False)


@pytest.mark.asyncio
async def test_search_tool_callable():
    """search tool (v2) should be callable as an async function."""
    mod = _import_mcp()
    with patch("opencode_search.mcp.handle_search_code",
               AsyncMock(return_value={"results": [], "elapsed_ms": 0.0,
                                       "query": "test", "projects_searched": 0})):
        result = await mod.search(query="test")
    assert result is not None


@pytest.mark.asyncio
async def test_overview_status_callable():
    """overview(what=status) tool (v2) should be callable."""
    mod = _import_mcp()
    with patch("opencode_search.mcp.handle_project_status",
               AsyncMock(return_value={"indexed": False, "path": "/tmp/x"})):
        result = await mod.overview(project_path="/tmp/x", what="status")
    assert result is not None


@pytest.mark.asyncio
async def test_overview_projects_callable():
    """overview(what=projects) tool (v2) should be callable."""
    mod = _import_mcp()
    with patch("opencode_search.mcp.handle_list_indexed_projects",
               AsyncMock(return_value={"projects": []})):
        result = await mod.overview(what="projects")
    assert result is not None


@pytest.mark.asyncio
async def test_manage_stop_watching_callable():
    """manage(action=stop_watching) tool (v2) should be callable."""
    mod = _import_mcp()
    with patch("opencode_search.mcp.handle_stop_watching",
               AsyncMock(return_value={"was_watching": False, "status": "stopped", "path": "/tmp/x"})):
        result = await mod.manage(project_path="/tmp/x", action="stop_watching")
    assert result is not None


# ---------------------------------------------------------------------------
# Startup GPU guard
# ---------------------------------------------------------------------------


def test_run_mcp_server_calls_assert_gpu_available():
    """run_mcp_server must call assert_gpu_available before starting the server."""
    mod = _import_mcp()
    called = {"yes": False}

    def _mock_assert():
        called["yes"] = True
        raise SystemExit(0)  # abort after guard so we don't actually run the server

    with patch("opencode_search.embeddings.assert_gpu_available", side_effect=_mock_assert), \
         pytest.raises(SystemExit):
        mod.run_mcp_server()

    assert called["yes"], "run_mcp_server must call assert_gpu_available()"


# ---------------------------------------------------------------------------
# Watcher resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_watchers_skips_when_no_watch_entries():
    """resume_watchers() must work with an empty registry without error."""
    mod = _import_mcp()
    assert hasattr(mod, "resume_watchers"), "mcp.py must expose resume_watchers()"

    with patch("opencode_search.config.load_registry", return_value={}):
        await mod.resume_watchers()  # Should not raise


@pytest.mark.asyncio
async def test_resume_watchers_starts_watcher_for_watched_entries():
    """resume_watchers() must call watcher_manager.start for each watched entry."""
    from opencode_search.config import ProjectEntry, get_project_db_path

    mod = _import_mcp()
    entry = ProjectEntry(
        path="/tmp/watched",
        db_path=get_project_db_path("/tmp/watched"),
        dims=768,
        watch=True,
    )

    started = {"calls": []}

    async def mock_start(root, *, on_change):
        started["calls"].append(root)
        return True

    with patch("opencode_search.config.load_registry", return_value={"/tmp/watched": entry}), \
         patch("opencode_search.handlers._common.load_registry", return_value={"/tmp/watched": entry}), \
         patch("opencode_search.handlers._watch.load_registry", return_value={"/tmp/watched": entry}), \
         patch("opencode_search.handlers._watch.watcher_manager.start", side_effect=mock_start):
        await mod.resume_watchers()

    assert "/tmp/watched" in started["calls"]


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_open_auto_starts_watch_for_indexed_project():
    mod = _import_mcp()

    with patch("opencode_search.mcp.resolve_indexed_project_path", return_value="/tmp/proj"), \
         patch("opencode_search.mcp.handle_ensure_project_watching",
               AsyncMock(return_value={"status": "ok"})) as mock_watch:
        response = await mod.client_open(_FakeRequest({"client_id": "client-a", "cwd": "/tmp/proj"}))

    assert response.status_code == 200
    mock_watch.assert_awaited_once_with("/tmp/proj", persist=False)


@pytest.mark.asyncio
async def test_client_close_marks_pending_disconnect_without_immediate_release():
    mod = _import_mcp()

    with patch("opencode_search.mcp._release_stale_project_watches", AsyncMock()), \
         patch.object(mod.runtime_state, "client_close", return_value="/tmp/proj") as mock_close, \
         patch("opencode_search.mcp.handle_release_project_watch",
               AsyncMock(return_value={"status": "stopped"})) as mock_release:
        response = await mod.client_close(_FakeRequest({"client_id": "client-a"}))

    assert response.status_code == 200
    mock_close.assert_called_once_with("client-a")
    mock_release.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_cleanup_loop_releases_project_watch_without_new_requests():
    mod = _import_mcp()

    with patch.object(mod.runtime_state, "releaseable_stale_projects", return_value=["/tmp/proj"]), \
         patch("opencode_search.mcp.handle_release_project_watch",
               AsyncMock(return_value={"status": "stopped"})) as mock_release:
        await mod._release_stale_project_watches()

    mock_release.assert_awaited_once_with("/tmp/proj")
