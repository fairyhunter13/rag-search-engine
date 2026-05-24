"""Tests for bridge-side daemon registration behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

# mcp_bridge imports mcp.client.session at module level; skip when mcp is absent.
pytest.importorskip("mcp", reason="mcp package not installed — run tests with .venv/bin/pytest")

from opencode_search import mcp_bridge


@pytest.mark.asyncio
async def test_register_bridge_client_includes_cwd():
    with patch("opencode_search.mcp_bridge.os.getcwd", return_value="/tmp/proj"), \
         patch("opencode_search.mcp_bridge._notify_daemon", AsyncMock()) as mock_notify:
        await mcp_bridge._register_bridge_client()

    mock_notify.assert_awaited_once_with(
        "/admin/client/open",
        {
            "client_id": mcp_bridge._bridge_client_id,
            "cwd": "/tmp/proj",
        },
    )
