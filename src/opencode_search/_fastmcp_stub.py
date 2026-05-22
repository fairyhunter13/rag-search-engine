"""Minimal FastMCP stub used when the optional `mcp` package is unavailable."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class FastMCPStub:
    """No-op decorator surface for import-time testability.

    Production code should never rely on this stub for actual server operation.
    """

    def __init__(self, *args: Any, missing_exc: Exception | None = None, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self._missing_exc = missing_exc

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            return fn

        return decorator

    def run(self, *args: Any, **kwargs: Any) -> None:
        raise ModuleNotFoundError(
            "The `mcp` package is required to run the MCP server. "
            'Install the full runtime with `pip install -e "src/[dev]"`.'
        ) from self._missing_exc
