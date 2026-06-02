"""Verify that the MCP bridge (mcp_bridge.py) exposes the v2 7-tool intent API.

The bridge forwards all calls to the HTTP daemon. It exposes the same 7 intent
tools as mcp.py: search, ask, graph, overview, build, federation, manage.

No running daemon or GPU is required — tool registration happens at import time.
"""
from __future__ import annotations

import asyncio

import pytest

# Skip when the `mcp` package (hard dep of mcp_bridge) is absent
pytest.importorskip(
    "mcp",
    reason="mcp package not installed — run tests with .venv/bin/pytest",
)

# ---------------------------------------------------------------------------
# Expected tool catalogue for the bridge (v2 intent API — 7 tools)
# ---------------------------------------------------------------------------

EXPECTED_BRIDGE_TOOLS: list[str] = [
    "search",
    "ask",
    "graph",
    "overview",
    "build",
    "federation",
    "manage",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_bridge_tool_names() -> set[str]:
    """Return the set of tool names registered on the bridge FastMCP instance."""
    from opencode_search.mcp_bridge import bridge

    tools = asyncio.run(bridge.list_tools())
    return {t.name for t in tools}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBridgeToolRegistration:
    def test_bridge_instance_exists(self) -> None:
        """The module-level `bridge` object must be a FastMCP instance."""
        import opencode_search.mcp_bridge as bridge_mod

        assert hasattr(bridge_mod, "bridge"), (
            "opencode_search.mcp_bridge must expose a 'bridge' attribute"
        )
        assert bridge_mod.bridge is not None

    def test_bridge_exposes_all_intent_tools(self) -> None:
        """All 7 v2 intent tools must be registered on the bridge."""
        registered = _get_bridge_tool_names()
        missing = [t for t in EXPECTED_BRIDGE_TOOLS if t not in registered]
        assert not missing, (
            f"Bridge tool(s) not registered: {missing}\n"
            f"Registered: {sorted(registered)}"
        )

    def test_bridge_tool_count_is_exactly_7(self) -> None:
        """The bridge must expose exactly 7 intent tools."""
        registered = _get_bridge_tool_names()
        assert len(registered) == 7, (
            f"Expected exactly 7 bridge tools, got {len(registered)}: {sorted(registered)}"
        )

    @pytest.mark.parametrize("tool_name", EXPECTED_BRIDGE_TOOLS)
    def test_individual_bridge_tool_registered(self, tool_name: str) -> None:
        """Each intent tool appears as a separate parametrized test for clear output."""
        registered = _get_bridge_tool_names()
        assert tool_name in registered, (
            f"Bridge tool '{tool_name}' not registered. "
            f"Registered: {sorted(registered)}"
        )

    def test_bridge_has_all_tool_functions_as_module_attributes(self) -> None:
        """Each bridge tool function must be a module-level attribute."""
        import opencode_search.mcp_bridge as bridge_mod

        for tool_name in EXPECTED_BRIDGE_TOOLS:
            assert hasattr(bridge_mod, tool_name), (
                f"opencode_search.mcp_bridge has no module-level attribute '{tool_name}'"
            )
            func = getattr(bridge_mod, tool_name)
            assert callable(func), f"bridge.{tool_name} must be callable"

    def test_bridge_search_has_project_paths_scoping(self) -> None:
        """search in bridge must accept project_paths kwarg (workspace auto-scoping)."""
        import inspect

        import opencode_search.mcp_bridge as bridge_mod

        sig = inspect.signature(bridge_mod.search)
        assert "project_paths" in sig.parameters, (
            "bridge.search must accept project_paths for workspace scoping"
        )

    def test_bridge_build_has_workspace_guard(self) -> None:
        """build in bridge must accept a project_path param (subject to workspace guard)."""
        import inspect

        import opencode_search.mcp_bridge as bridge_mod

        sig = inspect.signature(bridge_mod.build)
        assert "project_path" in sig.parameters, (
            "bridge.build must accept a 'project_path' parameter"
        )
