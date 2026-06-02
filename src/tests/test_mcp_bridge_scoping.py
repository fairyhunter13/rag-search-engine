"""Tests for mcp_bridge cwd-based scoping (stdio bridge behavior, v2 intent API)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("mcp", reason="mcp package not installed — run tests with .venv/bin/pytest")


@pytest.mark.asyncio
async def test_bridge_search_defaults_to_nearest_indexed_project(tmp_path, monkeypatch):
    """search tool auto-scopes project_paths to the nearest indexed project in cwd."""
    repo = tmp_path / "repo"
    subproj = repo / "subproj"
    deep = subproj / "src"
    deep.mkdir(parents=True)
    monkeypatch.setenv("OPENCODE_BRIDGE_WORKSPACE_ROOT", str(repo))
    monkeypatch.chdir(deep)

    from opencode_search import mcp_bridge

    async def fake_forward(name: str, arguments: dict):
        if name == "overview":
            # Called by _default_scoped_project_paths
            return {
                "projects": [
                    {"path": str(repo)},
                    {"path": str(subproj)},
                ]
            }
        if name == "search":
            # Should be scoped to the deepest matching project (subproj)
            assert arguments["project_paths"] == [str(subproj)]
            return {"results": [], "projects_searched": 1, "query": arguments["query"]}
        raise AssertionError(f"unexpected tool: {name}")

    with patch.object(mcp_bridge, "_forward_tool", AsyncMock(side_effect=fake_forward)):
        result = await mcp_bridge.search(query="registry path?")

    assert "error" not in result


@pytest.mark.asyncio
async def test_bridge_search_falls_back_to_global_when_no_matching_project(tmp_path, monkeypatch):
    """When cwd is not under any indexed project, search passes project_paths=None (global)."""
    cwd = tmp_path / "unindexed"
    cwd.mkdir()
    monkeypatch.setenv("OPENCODE_BRIDGE_WORKSPACE_ROOT", str(cwd))
    monkeypatch.chdir(cwd)

    from opencode_search import mcp_bridge

    async def fake_forward(name: str, arguments: dict):
        if name == "overview":
            return {"projects": [{"path": str(tmp_path / "somewhere-else")}]}
        if name == "search":
            # No project_paths scoped → None (global search)
            assert arguments["project_paths"] is None
            return {"results": [], "projects_searched": 0, "query": arguments["query"]}
        raise AssertionError(f"unexpected tool: {name}")

    with patch.object(mcp_bridge, "_forward_tool", AsyncMock(side_effect=fake_forward)):
        result = await mcp_bridge.search(query="anything")

    # No error — falls back to global search
    assert "error" not in result


@pytest.mark.asyncio
async def test_bridge_build_rejects_paths_outside_workspace_root(tmp_path, monkeypatch):
    """build tool rejects project_path outside the workspace root."""
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
    """build tool allows paths outside workspace when OPENCODE_ALLOW_INDEX_OUTSIDE_CWD=1."""
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
    """search tool rejects explicitly provided project_paths outside the workspace."""
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


