"""Drive an async MCP tool from a sync test."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

T = TypeVar("T")


def run_tool(coro: Coroutine[Any, Any, T]) -> T:
    # pytest-playwright's session-scoped browser leaves a loop running on this thread for the rest
    # of the session, so a bare asyncio.run() in any sync test ordered after the first browser test
    # raises RuntimeError -- 107 of the suite's 111 failures were this, and only when so ordered.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()
