"""Tests for mcp_bridge cwd-based scoping (stdio bridge behavior)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("mcp", reason="mcp package not installed — run tests with .venv/bin/pytest")


@pytest.mark.asyncio
async def test_bridge_search_code_defaults_to_nearest_indexed_project(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    subproj = repo / "subproj"
    deep = subproj / "src"
    deep.mkdir(parents=True)
    monkeypatch.setenv("OPENCODE_BRIDGE_WORKSPACE_ROOT", str(repo))
    monkeypatch.chdir(deep)

    from opencode_search import mcp_bridge

    async def fake_forward(name: str, arguments: dict):
        if name == "list_indexed_projects":
            return {
                "projects": [
                    {"path": str(repo)},
                    {"path": str(subproj)},
                ]
            }
        if name == "search_code":
            assert arguments["project_paths"] == [str(subproj)]
            return {"results": [], "projects_searched": 1, "query": arguments["query"]}
        raise AssertionError(f"unexpected tool: {name}")

    with patch.object(mcp_bridge, "_forward_tool", AsyncMock(side_effect=fake_forward)):
        result = await mcp_bridge.search_code(query="registry path?")

    assert "error" not in result


@pytest.mark.asyncio
async def test_bridge_search_code_errors_when_cwd_not_under_any_indexed_project(tmp_path, monkeypatch):
    cwd = tmp_path / "unindexed"
    cwd.mkdir()
    monkeypatch.setenv("OPENCODE_BRIDGE_WORKSPACE_ROOT", str(cwd))
    monkeypatch.chdir(cwd)

    from opencode_search import mcp_bridge

    async def fake_forward(name: str, arguments: dict):
        if name == "list_indexed_projects":
            return {"projects": [{"path": str(tmp_path / "somewhere-else")}]}
        raise AssertionError(f"unexpected tool: {name}")

    with patch.object(mcp_bridge, "_forward_tool", AsyncMock(side_effect=fake_forward)):
        result = await mcp_bridge.search_code(query="anything")

    assert result.get("status") == "error"
    assert "No indexed project contains the current working directory" in result.get("error", "")


@pytest.mark.asyncio
async def test_bridge_index_project_rejects_paths_outside_workspace_root(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    deep = repo / "src"
    deep.mkdir(parents=True)
    monkeypatch.setenv("OPENCODE_BRIDGE_WORKSPACE_ROOT", str(repo))
    monkeypatch.chdir(deep)

    from opencode_search import mcp_bridge

    other = tmp_path / "other"
    other.mkdir()

    with patch.object(mcp_bridge, "_forward_tool", AsyncMock()) as mock_forward:
        result = await mcp_bridge.index_project(path=str(other))

    assert result.get("status") == "error"
    assert "restricted to the currently opened workspace" in result.get("error", "")
    mock_forward.assert_not_awaited()


@pytest.mark.asyncio
async def test_bridge_index_project_allows_outside_when_override_set(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("OPENCODE_BRIDGE_WORKSPACE_ROOT", str(repo))
    monkeypatch.chdir(repo)

    from opencode_search import mcp_bridge

    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("OPENCODE_ALLOW_INDEX_OUTSIDE_CWD", "1")

    with patch.object(mcp_bridge, "_forward_tool", AsyncMock(return_value={"status": "ok"})) as mock_forward:
        result = await mcp_bridge.index_project(path=str(other))

    assert result.get("status") == "ok"
    mock_forward.assert_awaited_once()


@pytest.mark.asyncio
async def test_bridge_search_code_rejects_explicit_project_paths_outside_workspace(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("OPENCODE_BRIDGE_WORKSPACE_ROOT", str(repo))
    monkeypatch.chdir(repo)

    from opencode_search import mcp_bridge

    outside = tmp_path / "outside"
    outside.mkdir()

    with patch.object(mcp_bridge, "_forward_tool", AsyncMock()) as mock_forward:
        result = await mcp_bridge.search_code(query="anything", project_paths=[str(outside)])

    assert result.get("status") == "error"
    assert "restricted to the currently opened workspace" in result.get("error", "")
    mock_forward.assert_not_awaited()


@pytest.mark.asyncio
async def test_bridge_list_indexed_projects_filters_to_workspace_root(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("OPENCODE_BRIDGE_WORKSPACE_ROOT", str(repo))
    monkeypatch.chdir(repo)

    from opencode_search import mcp_bridge

    outside = tmp_path / "outside"
    outside.mkdir()

    async def fake_forward(name: str, arguments: dict):
        assert name == "list_indexed_projects"
        return {
            "projects": [
                {"path": str(repo), "tier": "balanced"},
                {"path": str(outside), "tier": "balanced"},
            ]
        }

    with patch.object(mcp_bridge, "_forward_tool", AsyncMock(side_effect=fake_forward)):
        result = await mcp_bridge.list_indexed_projects()

    paths = [p["path"] for p in result.get("projects", [])]
    assert paths == [str(repo)]
