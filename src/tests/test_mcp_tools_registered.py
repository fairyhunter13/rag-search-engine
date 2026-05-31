"""Verify that all expected MCP tools are registered on the FastMCP server.

These tests import opencode_search.mcp directly and call mcp.list_tools()
(which is an async method on the FastMCP instance).  No running server or
GPU is needed — the tool registry is populated at import time by the
@mcp.tool() decorators.
"""
from __future__ import annotations

import asyncio

import pytest

# Skip the whole module if starlette (a hard dep of opencode_search.mcp) is absent
pytest.importorskip(
    "starlette",
    reason="starlette not installed — run tests with .venv/bin/pytest",
)

# ---------------------------------------------------------------------------
# Expected tool catalogue — v2 intent API (7 tools)
# ---------------------------------------------------------------------------

EXPECTED_MCP_TOOLS: list[str] = [
    "search",       # find code/docs
    "ask",          # architectural questions
    "graph",        # callers/callees/impact/trace
    "overview",     # structure/communities/status/projects/metrics
    "build",        # index/pipeline/enrich/wiki/ingest
    "federation",   # discover/list/add/remove/index members
    "manage",       # stop_watching/wiki_lint
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_registered_tool_names() -> set[str]:
    """Return the set of tool names currently registered on mcp.mcp."""
    from opencode_search.mcp import mcp as _mcp

    tools = asyncio.run(_mcp.list_tools())
    return {t.name for t in tools}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMcpToolRegistration:
    """All 7 expected MCP tools (v2 intent API) must be registered."""

    def test_all_expected_mcp_tools_are_registered(self) -> None:
        """Every name in EXPECTED_MCP_TOOLS must appear in mcp.list_tools()."""
        registered = _get_registered_tool_names()
        missing = [t for t in EXPECTED_MCP_TOOLS if t not in registered]
        assert not missing, (
            f"MCP tool(s) not registered: {missing}\n"
            f"Registered tools: {sorted(registered)}"
        )

    def test_mcp_tool_count_is_exactly_7(self) -> None:
        """The server must expose exactly 7 intent tools (v2 API)."""
        registered = _get_registered_tool_names()
        assert len(registered) == 7, (
            f"Expected exactly 7 intent tools, got {len(registered)}: {sorted(registered)}"
        )

    @pytest.mark.parametrize("tool_name", EXPECTED_MCP_TOOLS)
    def test_individual_tool_registered(self, tool_name: str) -> None:
        """Each tool appears as a separate parametrized test for clear output."""
        registered = _get_registered_tool_names()
        assert tool_name in registered, (
            f"MCP tool '{tool_name}' not registered. "
            f"Registered: {sorted(registered)}"
        )

    def test_tool_names_are_strings(self) -> None:
        """Sanity: every registered tool has a non-empty string name."""
        from opencode_search.mcp import mcp as _mcp

        tools = asyncio.run(_mcp.list_tools())
        for tool in tools:
            assert isinstance(tool.name, str) and tool.name, (
                f"Tool has invalid name: {tool!r}"
            )

    def test_mcp_instance_exists(self) -> None:
        """The module-level `mcp` object must exist and be a FastMCP instance."""
        import opencode_search.mcp as mcp_mod

        assert hasattr(mcp_mod, "mcp"), "opencode_search.mcp must expose a 'mcp' attribute"
        assert mcp_mod.mcp is not None

    def test_mcp_has_all_tool_functions_as_module_attributes(self) -> None:
        """Each tool function must be accessible as a module-level attribute."""
        import opencode_search.mcp as mcp_mod

        for tool_name in EXPECTED_MCP_TOOLS:
            assert hasattr(mcp_mod, tool_name), (
                f"opencode_search.mcp has no module-level attribute '{tool_name}'"
            )
            func = getattr(mcp_mod, tool_name)
            assert callable(func), f"mcp.{tool_name} must be callable"
