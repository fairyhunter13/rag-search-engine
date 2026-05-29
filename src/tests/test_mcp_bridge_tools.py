"""Verify that the MCP bridge (mcp_bridge.py) exposes the expected tool surface.

The bridge intentionally omits the 4 daemon-internal tools
(project_status, list_indexed_projects, stop_watching, search_metrics)
and instead exposes 16 user-facing tools.

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
# Expected tool catalogue for the bridge
# ---------------------------------------------------------------------------

# Bridge exposes these 16 user-facing tools (omits the 4 daemon-admin tools
# present in mcp.py: project_status, list_indexed_projects, stop_watching,
# search_metrics — those are accessible only via the HTTP admin routes).
EXPECTED_BRIDGE_TOOLS: list[str] = [
    # Core
    "search_code",
    "index_project",
    # Graph / structural
    "get_symbol",
    "get_callers",
    "get_callees",
    "trace_path",
    "detect_impact",
    "get_communities",
    "global_search",
    # LLM enrichment
    "enrich_project",
    "get_symbol_intent",
    # Wiki
    "wiki_generate",
    "wiki_ingest",
    "wiki_query",
    "wiki_lint",
    # Docs
    "search_docs",
]

# Tools present in mcp.py but intentionally absent from the bridge
DAEMON_ADMIN_TOOLS: list[str] = [
    "project_status",
    "list_indexed_projects",
    "stop_watching",
    "search_metrics",
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

    def test_bridge_exposes_all_search_tools(self) -> None:
        """All expected bridge tools must be registered."""
        registered = _get_bridge_tool_names()
        missing = [t for t in EXPECTED_BRIDGE_TOOLS if t not in registered]
        assert not missing, (
            f"Bridge tool(s) not registered: {missing}\n"
            f"Registered: {sorted(registered)}"
        )

    def test_bridge_tool_count_is_at_least_16(self) -> None:
        """The bridge must expose at least 16 tools."""
        registered = _get_bridge_tool_names()
        assert len(registered) >= 16, (
            f"Expected >= 16 bridge tools, got {len(registered)}: {sorted(registered)}"
        )

    @pytest.mark.parametrize("tool_name", EXPECTED_BRIDGE_TOOLS)
    def test_individual_bridge_tool_registered(self, tool_name: str) -> None:
        """Each tool appears as a separate parametrized test for clear output."""
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

    def test_bridge_search_code_has_workspace_scoping(self) -> None:
        """search_code in bridge must accept project_paths kwarg (scoping)."""
        import inspect
        import opencode_search.mcp_bridge as bridge_mod

        sig = inspect.signature(bridge_mod.search_code)
        assert "project_paths" in sig.parameters, (
            "bridge.search_code must accept project_paths for workspace scoping"
        )

    def test_bridge_index_project_has_workspace_guard(self) -> None:
        """index_project in bridge must accept a path param (subject to workspace guard)."""
        import inspect
        import opencode_search.mcp_bridge as bridge_mod

        sig = inspect.signature(bridge_mod.index_project)
        assert "path" in sig.parameters, (
            "bridge.index_project must accept a 'path' parameter"
        )
