"""Minimal FastMCP stub — used when mcp.server.fastmcp is not importable."""
from __future__ import annotations

import asyncio
import json
import sys


class FastMCPStub:
    """Implements the FastMCP interface subset used by this server."""

    def __init__(self, name: str, instructions: str = "") -> None:
        self.name = name
        self.instructions = instructions
        self._tools: dict[str, object] = {}

    def tool(self):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn
        return decorator

    async def run_stdio_async(self) -> None:
        for line in sys.stdin:
            try:
                req = json.loads(line)
                name = req.get("method", "").replace("tools/call ", "")
                tool = self._tools.get(name)
                if tool:
                    args = req.get("params", {}).get("arguments", {})
                    result = await tool(**args) if asyncio.iscoroutinefunction(tool) else tool(**args)
                    print(json.dumps({"result": result}), flush=True)
                else:
                    print(json.dumps({"error": f"unknown tool: {name}"}), flush=True)
            except Exception as exc:
                print(json.dumps({"error": str(exc)}), flush=True)

    def streamable_http_app(self):
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def _dispatch(request):
            body = await request.json()
            name = body.get("tool", "")
            tool = self._tools.get(name)
            if not tool:
                return JSONResponse({"error": f"unknown tool: {name}"}, status_code=404)
            args = body.get("arguments", {})
            result = await tool(**args) if asyncio.iscoroutinefunction(tool) else tool(**args)
            return JSONResponse({"result": result})

        return Starlette(routes=[Route("/mcp", _dispatch, methods=["POST"])])
