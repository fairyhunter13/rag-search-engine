"""Tests for opencode_search.mcp — tool registration and server setup.

These tests verify that:
- FastMCP server imports cleanly
- All 5 tools are registered
- Tool names are correct
- run_mcp_server is callable

No GPU required (GPU guard is patched in conftest).
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


def _import_mcp():
    """Import the mcp module, which triggers FastMCP server instantiation."""
    import importlib
    if "opencode_search.mcp" in __import__("sys").modules:
        return __import__("sys").modules["opencode_search.mcp"]
    return importlib.import_module("opencode_search.mcp")


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


def test_mcp_has_index_project_tool():
    mod = _import_mcp()
    assert hasattr(mod, "index_project")


def test_mcp_has_search_code_tool():
    mod = _import_mcp()
    assert hasattr(mod, "search_code")


def test_mcp_has_project_status_tool():
    mod = _import_mcp()
    assert hasattr(mod, "project_status")


def test_mcp_has_list_indexed_projects_tool():
    mod = _import_mcp()
    assert hasattr(mod, "list_indexed_projects")


def test_mcp_has_stop_watching_tool():
    mod = _import_mcp()
    assert hasattr(mod, "stop_watching")


def test_mcp_has_run_mcp_server():
    mod = _import_mcp()
    assert callable(mod.run_mcp_server)


# ---------------------------------------------------------------------------
# Tool callability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_project_tool_callable():
    """index_project tool should be callable as an async function."""
    mod = _import_mcp()
    with patch("opencode_search.mcp.handle_index_project",
               AsyncMock(return_value={"status": "ok", "files_indexed": 0,
                                       "files_unchanged": 0, "files_removed": 0,
                                       "chunks_total": 0, "errors": 0,
                                       "elapsed_s": 0.1, "watching": False,
                                       "path": "/tmp/x", "tier": "balanced"})):
        result = await mod.index_project(path="/tmp/x", tier="balanced")
    assert "status" in result or result is not None


@pytest.mark.asyncio
async def test_search_code_tool_callable():
    """search_code tool should be callable as an async function."""
    mod = _import_mcp()
    with patch("opencode_search.mcp.handle_search_code",
               AsyncMock(return_value={"results": [], "elapsed_ms": 0.0,
                                       "query": "test", "projects_searched": 0})):
        result = await mod.search_code(query="test")
    assert "results" in result or result is not None


@pytest.mark.asyncio
async def test_project_status_tool_callable():
    mod = _import_mcp()
    with patch("opencode_search.mcp.handle_project_status",
               AsyncMock(return_value={"indexed": False, "path": "/tmp/x"})):
        result = await mod.project_status(path="/tmp/x")
    assert result is not None


@pytest.mark.asyncio
async def test_list_indexed_projects_tool_callable():
    mod = _import_mcp()
    with patch("opencode_search.mcp.handle_list_indexed_projects",
               AsyncMock(return_value={"projects": []})):
        result = await mod.list_indexed_projects()
    assert "projects" in result or result is not None


@pytest.mark.asyncio
async def test_stop_watching_tool_callable():
    mod = _import_mcp()
    with patch("opencode_search.mcp.handle_stop_watching",
               AsyncMock(return_value={"was_watching": False, "status": "stopped", "path": "/tmp/x"})):
        result = await mod.stop_watching(path="/tmp/x")
    assert result is not None


# ---------------------------------------------------------------------------
# Startup GPU guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_calls_assert_gpu_available():
    """_startup() must call assert_gpu_available (GPU enforcement guard)."""
    mod = _import_mcp()
    assert hasattr(mod, "_startup"), "mcp.py must expose _startup() coroutine"

    gpu_checked = {"called": False}

    def mock_assert():
        gpu_checked["called"] = True

    # Patch at the opencode_search.embeddings module level (where _startup imports it)
    with patch("opencode_search.embeddings.assert_gpu_available", side_effect=mock_assert), \
         patch("opencode_search.config.load_registry", return_value={}):
        try:
            await mod._startup()
        except Exception:
            pass  # Registry load or watcher start may fail in test environment

    assert gpu_checked["called"], "_startup() must call assert_gpu_available()"
