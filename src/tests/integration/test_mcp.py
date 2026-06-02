"""Tests for opencode_search.mcp and mcp_bridge — tool registration, server setup, contracts, bridge behavior, and runtime e2e."""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("starlette", reason="starlette not installed — run tests with .venv/bin/pytest")


def _import_mcp():
    import importlib
    if "opencode_search.mcp" in __import__("sys").modules:
        return __import__("sys").modules["opencode_search.mcp"]
    return importlib.import_module("opencode_search.mcp")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _mock_note_activity():
    from opencode_search import mcp as mcp_mod
    return patch.object(mcp_mod.runtime_state, "note_activity")


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


# === Core MCP ===

def test_mcp_imports():
    mod = _import_mcp()
    assert mod is not None


def test_mcp_server_instance():
    mod = _import_mcp()
    assert hasattr(mod, "mcp")
    assert mod.mcp is not None


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


@pytest.mark.asyncio
async def test_build_index_callable():
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
    mod = _import_mcp()
    with patch("opencode_search.mcp.handle_search_code",
               AsyncMock(return_value={"results": [], "elapsed_ms": 0.0,
                                       "query": "test", "projects_searched": 0})):
        result = await mod.search(query="test")
    assert result is not None


@pytest.mark.asyncio
async def test_overview_status_callable():
    mod = _import_mcp()
    with patch("opencode_search.mcp.handle_project_status",
               AsyncMock(return_value={"indexed": False, "path": "/tmp/x"})):
        result = await mod.overview(project_path="/tmp/x", what="status")
    assert result is not None


@pytest.mark.asyncio
async def test_overview_projects_callable():
    mod = _import_mcp()
    with patch("opencode_search.mcp.handle_list_indexed_projects",
               AsyncMock(return_value={"projects": []})):
        result = await mod.overview(what="projects")
    assert result is not None


@pytest.mark.asyncio
async def test_manage_stop_watching_callable():
    mod = _import_mcp()
    with patch("opencode_search.mcp.handle_stop_watching",
               AsyncMock(return_value={"was_watching": False, "status": "stopped", "path": "/tmp/x"})):
        result = await mod.manage(project_path="/tmp/x", action="stop_watching")
    assert result is not None


def test_run_mcp_server_calls_assert_gpu_available():
    mod = _import_mcp()
    called = {"yes": False}

    def _mock_assert():
        called["yes"] = True
        raise SystemExit(0)

    with patch("opencode_search.embeddings.assert_gpu_available", side_effect=_mock_assert), \
         pytest.raises(SystemExit):
        mod.run_mcp_server()

    assert called["yes"], "run_mcp_server must call assert_gpu_available()"


@pytest.mark.asyncio
async def test_resume_watchers_skips_when_no_watch_entries():
    mod = _import_mcp()
    assert hasattr(mod, "resume_watchers"), "mcp.py must expose resume_watchers()"

    with patch("opencode_search.config.load_registry", return_value={}):
        await mod.resume_watchers()


@pytest.mark.asyncio
async def test_resume_watchers_starts_watcher_for_watched_entries():
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


# === Tool Registration ===

EXPECTED_MCP_TOOLS: list[str] = [
    "search", "ask", "graph", "overview", "build", "federation", "manage",
]


def _get_registered_tool_names() -> set[str]:
    from opencode_search.mcp import mcp as _mcp
    tools = asyncio.run(_mcp.list_tools())
    return {t.name for t in tools}


class TestMcpToolRegistration:
    def test_all_expected_mcp_tools_are_registered(self) -> None:
        registered = _get_registered_tool_names()
        missing = [t for t in EXPECTED_MCP_TOOLS if t not in registered]
        assert not missing, (
            f"MCP tool(s) not registered: {missing}\nRegistered tools: {sorted(registered)}"
        )

    def test_mcp_tool_count_is_exactly_7(self) -> None:
        registered = _get_registered_tool_names()
        assert len(registered) == 7, (
            f"Expected exactly 7 intent tools, got {len(registered)}: {sorted(registered)}"
        )

    @pytest.mark.parametrize("tool_name", EXPECTED_MCP_TOOLS)
    def test_individual_tool_registered(self, tool_name: str) -> None:
        registered = _get_registered_tool_names()
        assert tool_name in registered, (
            f"MCP tool '{tool_name}' not registered. Registered: {sorted(registered)}"
        )

    def test_tool_names_are_strings(self) -> None:
        from opencode_search.mcp import mcp as _mcp
        tools = asyncio.run(_mcp.list_tools())
        for tool in tools:
            assert isinstance(tool.name, str) and tool.name

    def test_mcp_instance_exists(self) -> None:
        import opencode_search.mcp as mcp_mod
        assert hasattr(mcp_mod, "mcp")
        assert mcp_mod.mcp is not None

    def test_mcp_has_all_tool_functions_as_module_attributes(self) -> None:
        import opencode_search.mcp as mcp_mod
        for tool_name in EXPECTED_MCP_TOOLS:
            assert hasattr(mcp_mod, tool_name)
            func = getattr(mcp_mod, tool_name)
            assert callable(func)


# === MCP Contracts ===

class TestSearchContracts:
    def test_search_valid_scope_returns_results_key(self):
        from opencode_search import mcp as mcp_mod
        with _mock_note_activity(), patch("opencode_search.mcp.handle_search_code") as m:
            m.return_value = {"results": [], "total": 0}
            result = _run(mcp_mod.search(query="test", scope="code"))
        assert isinstance(result, dict)
        assert "results" in result or "error" in result

    def test_search_invalid_scope_returns_error(self):
        from opencode_search import mcp as mcp_mod
        with _mock_note_activity():
            result = _run(mcp_mod.search(query="test", scope="totally_invalid_scope"))
        assert "error" in result

    def test_search_each_valid_scope_accepted(self):
        from opencode_search import mcp as mcp_mod
        for scope in ("code", "docs", "all", "similar"):
            with _mock_note_activity(), patch("opencode_search.mcp.handle_search_code") as m:
                m.return_value = {"results": [], "total": 0}
                result = _run(mcp_mod.search(query="test", scope=scope))
            assert "error" not in result or result.get("error") == ""


class TestGraphContracts:
    def test_graph_invalid_relation_returns_error(self):
        from opencode_search import mcp as mcp_mod
        with _mock_note_activity():
            result = _run(mcp_mod.graph(symbol="foo", project_path="/tmp/nonexistent", relation="invalid_relation"))
        assert "error" in result

    def test_graph_path_requires_to_symbol(self):
        from opencode_search import mcp as mcp_mod
        with _mock_note_activity():
            result = _run(mcp_mod.graph(symbol="foo", project_path="/tmp/nonexistent", relation="path", to_symbol=""))
        assert isinstance(result, dict)

    def test_graph_valid_relations_accepted(self):
        from opencode_search import mcp as mcp_mod
        for relation in ("definition", "callers", "callees", "impact"):
            with (_mock_note_activity(),
                  patch("opencode_search.mcp.handle_get_symbol") as ms,
                  patch("opencode_search.mcp.handle_get_callers") as mc,
                  patch("opencode_search.mcp.handle_get_callees") as mce,
                  patch("opencode_search.mcp.handle_detect_impact") as mi):
                ms.return_value = {"matches": []}
                mc.return_value = {"callers": []}
                mce.return_value = {"callees": []}
                mi.return_value = {"impact": []}
                result = _run(mcp_mod.graph(symbol="test", project_path="/tmp/x", relation=relation))
            assert isinstance(result, dict)
            assert "error" not in result or "invalid" not in result.get("error", "").lower()


class TestOverviewContracts:
    def test_overview_invalid_what_returns_error(self):
        from opencode_search import mcp as mcp_mod
        with _mock_note_activity():
            result = _run(mcp_mod.overview(project_path="/tmp/nonexistent", what="invalid_what"))
        assert "error" in result

    def test_overview_projects_what_does_not_require_project_path(self):
        from opencode_search import mcp as mcp_mod
        with _mock_note_activity(), patch("opencode_search.mcp.handle_list_indexed_projects") as m:
            m.return_value = {"projects": []}
            result = _run(mcp_mod.overview(what="projects"))
        assert isinstance(result, dict)


class TestBuildContracts:
    def test_build_invalid_action_returns_error(self):
        from opencode_search import mcp as mcp_mod
        with _mock_note_activity():
            result = _run(mcp_mod.build(project_path="/tmp/x", action="not_a_real_action"))
        assert "error" in result
        assert "valid" in str(result).lower() or "valid_actions" in result

    def test_build_ingest_without_source_path_returns_error(self):
        from opencode_search import mcp as mcp_mod
        with _mock_note_activity():
            result = _run(mcp_mod.build(project_path="/tmp/x", action="ingest"))
        assert "error" in result
        assert "source_path" in result.get("error", "")

    def test_build_describe_symbol_without_symbol_returns_error(self):
        from opencode_search import mcp as mcp_mod
        with _mock_note_activity():
            result = _run(mcp_mod.build(project_path="/tmp/x", action="describe_symbol"))
        assert "error" in result


class TestFederationContracts:
    def test_federation_invalid_action_returns_error(self):
        from opencode_search import mcp as mcp_mod
        with _mock_note_activity():
            result = _run(mcp_mod.federation(root_path="/tmp/x", action="invalid_action"))
        assert "error" in result


class TestManageContracts:
    def test_manage_invalid_action_returns_error(self):
        from opencode_search import mcp as mcp_mod
        with _mock_note_activity():
            result = _run(mcp_mod.manage(project_path="/tmp/x", action="invalid_action"))
        assert "error" in result

    def test_manage_valid_actions_accepted(self):
        from opencode_search import mcp as mcp_mod
        for action in ("stop_watching", "wiki_lint"):
            with (_mock_note_activity(),
                  patch("opencode_search.mcp.handle_stop_watching") as ms,
                  patch("opencode_search.mcp.handle_wiki_lint") as mw):
                ms.return_value = {"status": "ok"}
                mw.return_value = {"issues": [], "orphans": []}
                result = _run(mcp_mod.manage(project_path="/tmp/x", action=action))
            assert isinstance(result, dict)
            assert "error" not in result or "invalid" not in result.get("error", "").lower()


class TestCrossToolContracts:
    def test_all_tools_return_dict(self):
        from opencode_search import mcp as mcp_mod
        calls = [
            lambda: _run(mcp_mod.search(query="x", scope="invalid_xyz")),
            lambda: _run(mcp_mod.graph(symbol="x", project_path="/tmp", relation="invalid_xyz")),
            lambda: _run(mcp_mod.overview(project_path="/tmp", what="invalid_xyz")),
            lambda: _run(mcp_mod.build(project_path="/tmp", action="invalid_xyz")),
            lambda: _run(mcp_mod.federation(root_path="/tmp", action="invalid_xyz")),
            lambda: _run(mcp_mod.manage(project_path="/tmp", action="invalid_xyz")),
        ]
        with _mock_note_activity():
            for call in calls:
                result = call()
                assert isinstance(result, dict)

    def test_all_tools_have_note_activity_called(self):
        from opencode_search import mcp as mcp_mod
        activity_calls = []
        with patch.object(mcp_mod.runtime_state, "note_activity",
                          side_effect=lambda: activity_calls.append(1)):
            _run(mcp_mod.search(query="test", scope="invalid"))
        assert len(activity_calls) >= 1


# === Bridge ===

_mcp_pkg = pytest.importorskip("mcp", reason="mcp package not installed — run tests with .venv/bin/pytest")


@pytest.mark.asyncio
async def test_register_bridge_client_includes_cwd():
    from opencode_search import mcp_bridge
    with patch("opencode_search.mcp_bridge.os.getcwd", return_value="/tmp/proj"), \
         patch("opencode_search.mcp_bridge._notify_daemon", AsyncMock()) as mock_notify:
        await mcp_bridge._register_bridge_client()

    mock_notify.assert_awaited_once_with(
        "/admin/client/open",
        {"client_id": mcp_bridge._bridge_client_id, "cwd": "/tmp/proj"},
    )


# === Bridge Tools ===

EXPECTED_BRIDGE_TOOLS: list[str] = [
    "search", "ask", "graph", "overview", "build", "federation", "manage",
]


def _get_bridge_tool_names() -> set[str]:
    from opencode_search.mcp_bridge import bridge
    tools = asyncio.run(bridge.list_tools())
    return {t.name for t in tools}


class TestBridgeToolRegistration:
    def test_bridge_instance_exists(self) -> None:
        import opencode_search.mcp_bridge as bridge_mod
        assert hasattr(bridge_mod, "bridge")
        assert bridge_mod.bridge is not None

    def test_bridge_exposes_all_intent_tools(self) -> None:
        registered = _get_bridge_tool_names()
        missing = [t for t in EXPECTED_BRIDGE_TOOLS if t not in registered]
        assert not missing, f"Bridge tool(s) not registered: {missing}"

    def test_bridge_tool_count_is_exactly_7(self) -> None:
        registered = _get_bridge_tool_names()
        assert len(registered) == 7

    @pytest.mark.parametrize("tool_name", EXPECTED_BRIDGE_TOOLS)
    def test_individual_bridge_tool_registered(self, tool_name: str) -> None:
        registered = _get_bridge_tool_names()
        assert tool_name in registered

    def test_bridge_has_all_tool_functions_as_module_attributes(self) -> None:
        import opencode_search.mcp_bridge as bridge_mod
        for tool_name in EXPECTED_BRIDGE_TOOLS:
            assert hasattr(bridge_mod, tool_name)
            assert callable(getattr(bridge_mod, tool_name))

    def test_bridge_search_has_project_paths_scoping(self) -> None:
        import opencode_search.mcp_bridge as bridge_mod
        sig = inspect.signature(bridge_mod.search)
        assert "project_paths" in sig.parameters

    def test_bridge_build_has_workspace_guard(self) -> None:
        import opencode_search.mcp_bridge as bridge_mod
        sig = inspect.signature(bridge_mod.build)
        assert "project_path" in sig.parameters


# === Bridge Scoping ===

@pytest.mark.asyncio
async def test_bridge_search_defaults_to_nearest_indexed_project(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    subproj = repo / "subproj"
    deep = subproj / "src"
    deep.mkdir(parents=True)
    monkeypatch.setenv("OPENCODE_BRIDGE_WORKSPACE_ROOT", str(repo))
    monkeypatch.chdir(deep)

    from opencode_search import mcp_bridge

    async def fake_forward(name: str, arguments: dict):
        if name == "overview":
            return {"projects": [{"path": str(repo)}, {"path": str(subproj)}]}
        if name == "search":
            assert arguments["project_paths"] == [str(subproj)]
            return {"results": [], "projects_searched": 1, "query": arguments["query"]}
        raise AssertionError(f"unexpected tool: {name}")

    with patch.object(mcp_bridge, "_forward_tool", AsyncMock(side_effect=fake_forward)):
        result = await mcp_bridge.search(query="registry path?")

    assert "error" not in result


@pytest.mark.asyncio
async def test_bridge_search_falls_back_to_global_when_no_matching_project(tmp_path, monkeypatch):
    cwd = tmp_path / "unindexed"
    cwd.mkdir()
    monkeypatch.setenv("OPENCODE_BRIDGE_WORKSPACE_ROOT", str(cwd))
    monkeypatch.chdir(cwd)

    from opencode_search import mcp_bridge

    async def fake_forward(name: str, arguments: dict):
        if name == "overview":
            return {"projects": [{"path": str(tmp_path / "somewhere-else")}]}
        if name == "search":
            assert arguments["project_paths"] is None
            return {"results": [], "projects_searched": 0, "query": arguments["query"]}
        raise AssertionError(f"unexpected tool: {name}")

    with patch.object(mcp_bridge, "_forward_tool", AsyncMock(side_effect=fake_forward)):
        result = await mcp_bridge.search(query="anything")

    assert "error" not in result


@pytest.mark.asyncio
async def test_bridge_build_rejects_paths_outside_workspace_root(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    deep = repo / "src"
    deep.mkdir(parents=True)
    monkeypatch.setenv("OPENCODE_BRIDGE_WORKSPACE_ROOT", str(repo))
    monkeypatch.delenv("OPENCODE_ALLOW_INDEX_OUTSIDE_CWD", raising=False)
    monkeypatch.chdir(deep)

    from opencode_search import mcp_bridge
    other = tmp_path / "other"
    other.mkdir()

    with patch.object(mcp_bridge, "_forward_tool", AsyncMock()) as mock_forward:
        result = await mcp_bridge.build(project_path=str(other), action="index")

    assert result.get("status") == "error"
    assert "restricted to the currently opened workspace" in result.get("error", "")
    mock_forward.assert_not_awaited()


@pytest.mark.asyncio
async def test_bridge_build_allows_outside_when_override_set(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("OPENCODE_BRIDGE_WORKSPACE_ROOT", str(repo))
    monkeypatch.chdir(repo)
    monkeypatch.setenv("OPENCODE_ALLOW_INDEX_OUTSIDE_CWD", "1")

    from opencode_search import mcp_bridge
    other = tmp_path / "other"
    other.mkdir()

    with patch.object(mcp_bridge, "_forward_tool", AsyncMock(return_value={"status": "ok"})) as mock_forward:
        result = await mcp_bridge.build(project_path=str(other), action="index")

    assert result.get("status") == "ok"
    mock_forward.assert_awaited_once()


@pytest.mark.asyncio
async def test_bridge_search_rejects_explicit_project_paths_outside_workspace(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("OPENCODE_BRIDGE_WORKSPACE_ROOT", str(repo))
    monkeypatch.delenv("OPENCODE_ALLOW_INDEX_OUTSIDE_CWD", raising=False)
    monkeypatch.chdir(repo)

    from opencode_search import mcp_bridge
    outside = tmp_path / "outside"
    outside.mkdir()

    with patch.object(mcp_bridge, "_forward_tool", AsyncMock()) as mock_forward:
        result = await mcp_bridge.search(query="anything", project_paths=[str(outside)])

    assert result.get("status") == "error"
    assert "restricted to the currently opened workspace" in result.get("error", "")
    mock_forward.assert_not_awaited()


# === E2E Runtime (integration + runtime_deps + gpu) ===

pytest.importorskip("lancedb")
pytest.importorskip("pyarrow")

from opencode_search import config as _config
from opencode_search.mcp import (
    _release_stale_project_watches,
    build,
    client_close,
    client_open,
    manage,
    overview,
    resume_watchers,
    search,
)
from opencode_search.search import clear_search_cache
from opencode_search.watcher import watcher_manager

_e2e_pytestmark = [pytest.mark.integration, pytest.mark.runtime_deps, pytest.mark.gpu]


async def _index_project(path, watch=False, force=False, follow_symlinks=True):
    return await build(project_path=path, action="index", watch=watch, force=force)


async def _search_code(query, project_paths=None, top_k=10, use_rerank=True):
    return await search(query=query, project_paths=project_paths, top_k=top_k)


async def _list_indexed_projects():
    return await overview(what="projects")


async def _project_status(path):
    return await overview(project_path=path, what="status")


async def _stop_watching(path):
    return await manage(project_path=path, action="stop_watching")


async def _wait_for_search_result(project_root, query, *, expected_substring, timeout_s=15.0):
    import asyncio as _asyncio
    deadline = _asyncio.get_running_loop().time() + timeout_s
    while _asyncio.get_running_loop().time() < deadline:
        clear_search_cache()
        result = await _search_code(query=query, project_paths=[str(project_root)], top_k=5, use_rerank=False)
        if any(expected_substring in row.get("content", "") for row in result.get("results", [])):
            return result
        await _asyncio.sleep(0.5)
    raise AssertionError(f"query {query!r} never returned content containing {expected_substring!r}")


async def _wait_for_search_absence(project_root, query, *, absent_substring, timeout_s=15.0):
    import asyncio as _asyncio
    deadline = _asyncio.get_running_loop().time() + timeout_s
    last_result = None
    while _asyncio.get_running_loop().time() < deadline:
        clear_search_cache()
        result = await _search_code(query=query, project_paths=[str(project_root)], top_k=5, use_rerank=False)
        last_result = result
        if not any(absent_substring in row.get("content", "") for row in result.get("results", [])):
            return result
        await _asyncio.sleep(0.5)
    raise AssertionError(f"query {query!r} kept returning content containing {absent_substring!r}: {last_result}")


async def _index_and_wait_e2e(path, timeout_s=120.0, **kwargs):
    result = await _index_project(path=path, **kwargs)
    assert result["status"] == "indexing", f"expected 'indexing', got {result}"
    import asyncio as _asyncio
    deadline = _asyncio.get_running_loop().time() + timeout_s
    while _asyncio.get_running_loop().time() < deadline:
        st = await _project_status(path=path)
        if st.get("indexed") and not st.get("indexing_running"):
            return st
        if not st.get("indexing_running") and st.get("indexed") is False:
            from pathlib import Path as _P
            from opencode_search.handlers import _indexing_status
            ps = str(_P(path).expanduser().resolve())
            final = _indexing_status.get(ps, {})
            if final.get("status") == "error":
                return final
        await _asyncio.sleep(0.5)
    raise AssertionError(f"indexing did not complete within {timeout_s}s")


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.runtime_deps
@pytest.mark.gpu
async def test_mcp_tools_real_end_to_end(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    source_file = project_root / "app.py"
    source_file.write_text(
        "SMOKE_MCP_INITIAL = 'mcp_alpha_unique'\n"
        "def initial_token():\n"
        "    return SMOKE_MCP_INITIAL\n"
    )
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(_config, "REGISTRY_PATH", registry_path)
    await watcher_manager.stop_all()
    await resume_watchers()
    try:
        indexed = await _index_and_wait_e2e(str(project_root), watch=True)
        assert indexed.get("watching") is True
        status = await _project_status(path=str(project_root))
        assert status["indexed"] is True and status["watching"] is True
        listed = await _list_indexed_projects()
        assert any(p["path"] == str(project_root) for p in listed["projects"])
        initial = await _wait_for_search_result(project_root, "mcp_alpha_unique", expected_substring="mcp_alpha_unique")
        assert any("mcp_alpha_unique" in row["content"] for row in initial["results"])
        import asyncio as _asyncio
        await _asyncio.sleep(1.0)
        source_file.write_text("SMOKE_MCP_UPDATED = 'mcp_beta_unique'\ndef rotated_token():\n    return SMOKE_MCP_UPDATED\n")
        updated = await _wait_for_search_result(project_root, "mcp_beta_unique", expected_substring="mcp_beta_unique")
        assert any("mcp_beta_unique" in row["content"] for row in updated["results"])
        stopped = await _stop_watching(path=str(project_root))
        assert stopped["status"] == "stopped" and stopped["was_watching"] is True
        source_file.write_text("SMOKE_MCP_STOPPED = 'mcp_gamma_unique'\n")
        await _asyncio.sleep(2.0)
        clear_search_cache()
        after_stop = await _search_code(query="mcp_gamma_unique", project_paths=[str(project_root)], top_k=5, use_rerank=False)
        assert not any("mcp_gamma_unique" in row.get("content", "") for row in after_stop.get("results", []))
    finally:
        await watcher_manager.stop_all()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.runtime_deps
@pytest.mark.gpu
async def test_mcp_resumes_persisted_watcher_and_removes_deleted_files(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    source_file = project_root / "app.py"
    deleted_file = project_root / "delete_me.py"
    source_file.write_text("SMOKE_MCP_RESUME_INITIAL = 'mcp_resume_alpha_unique'\n")
    deleted_file.write_text("SMOKE_MCP_DELETE = 'mcp_delete_unique'\n")
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(_config, "REGISTRY_PATH", registry_path)
    await watcher_manager.stop_all()
    await resume_watchers()
    try:
        indexed = await _index_and_wait_e2e(str(project_root), watch=True)
        assert indexed.get("watching") is True
        await _wait_for_search_result(project_root, "mcp_resume_alpha_unique", expected_substring="mcp_resume_alpha_unique")
        await _wait_for_search_result(project_root, "mcp_delete_unique", expected_substring="mcp_delete_unique")
        await watcher_manager.stop_all()
        assert watcher_manager.list_active() == []
        await resume_watchers()
        status = await _project_status(path=str(project_root))
        assert status["watching"] is True
        source_file.write_text("SMOKE_MCP_RESUME_UPDATED = 'mcp_resume_beta_unique'\n")
        await _wait_for_search_result(project_root, "mcp_resume_beta_unique", expected_substring="mcp_resume_beta_unique")
        deleted_file.unlink()
        await _wait_for_search_absence(project_root, "mcp_delete_unique", absent_substring="mcp_delete_unique")
        stopped = await _stop_watching(path=str(project_root))
        assert stopped["status"] == "stopped" and stopped["was_watching"] is True
    finally:
        await watcher_manager.stop_all()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.runtime_deps
@pytest.mark.gpu
async def test_mcp_first_use_index_auto_watch_and_background_release(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    nested = project_root / "nested"
    nested.mkdir(parents=True)
    source_file = project_root / "app.py"
    source_file.write_text("SMOKE_MCP_FIRST_USE = 'mcp_first_use_unique'\n")
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(_config, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr("opencode_search.mcp.DEFAULT_CLIENT_STALE_S", 1)
    await watcher_manager.stop_all()
    await resume_watchers()
    try:
        open_response = await client_open(_FakeRequest({"client_id": "client-a", "cwd": str(nested)}))
        assert open_response.status_code == 200
        await _index_and_wait_e2e(str(project_root), watch=False)
        status = await _project_status(path=str(project_root))
        assert status["indexed"] is True and status["watching"] is True
        assert str(project_root) in watcher_manager.list_active()
        await _wait_for_search_result(project_root, "mcp_first_use_unique", expected_substring="mcp_first_use_unique")
        close_response = await client_close(_FakeRequest({"client_id": "client-a"}))
        assert close_response.status_code == 200
        assert str(project_root) in watcher_manager.list_active()
        import asyncio as _asyncio
        deadline = _asyncio.get_running_loop().time() + 6.0
        while _asyncio.get_running_loop().time() < deadline:
            if str(project_root) not in watcher_manager.list_active():
                break
            await _release_stale_project_watches()
            await _asyncio.sleep(0.2)
        else:
            raise AssertionError("auto-started watcher was not released after client disconnect")
    finally:
        await watcher_manager.stop_all()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.runtime_deps
@pytest.mark.gpu
async def test_mcp_migrates_legacy_registry_db_path_before_resuming_watchers(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    source_file = project_root / "app.py"
    source_file.write_text("SMOKE_MCP_LEGACY = 'mcp_legacy_unique'\n")
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(_config, "REGISTRY_PATH", registry_path)
    await watcher_manager.stop_all()
    await resume_watchers()
    try:
        await _index_and_wait_e2e(str(project_root), watch=True)
        canonical_db_path = Path(_config.get_project_db_path(project_root))
        legacy_db_path = project_root / ".opencode" / "index_old"
        canonical_db_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_db_path.parent.mkdir(parents=True, exist_ok=True)
        await watcher_manager.stop_all()
        assert watcher_manager.list_active() == []
        canonical_db_path.rename(legacy_db_path)
        registry_path.write_text(
            json.dumps({str(project_root): {"path": str(project_root), "db_path": str(legacy_db_path), "dims": _config.DEFAULT_DIMS, "watch": True}}, indent=2),
            encoding="utf-8",
        )
        await resume_watchers()
        status = await _project_status(path=str(project_root))
        assert status["indexed"] is True and status["watching"] is True
        assert Path(status["db_path"]) == canonical_db_path
        assert canonical_db_path.exists()
        assert not legacy_db_path.exists()
        resumed = await _wait_for_search_result(project_root, "mcp_legacy_unique", expected_substring="mcp_legacy_unique")
        assert any("mcp_legacy_unique" in row["content"] for row in resumed["results"])
    finally:
        await watcher_manager.stop_all()
