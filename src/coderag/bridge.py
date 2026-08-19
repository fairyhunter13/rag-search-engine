"""stdio <-> HTTP, for the clients that cannot speak streamable HTTP.

Claude Code connects to `/mcp` directly. The other IDE clients this fleet wires
still spawn a subprocess and talk JSON-RPC over stdin/stdout, so they get a
process that forwards to the one daemon rather than a second daemon each -- five
editors holding five copies of a 12 GB model is the failure this prevents.

It is a pipe and nothing else: no retry, no queue, no cache. A bridge that
interprets the protocol is a second implementation of it, and the first thing
that drifts is the part nobody tests.
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from . import config


async def _pump(url: str, idle: float) -> int:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

    async with httpx.AsyncClient(timeout=None) as client:
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=idle or None)
            except TimeoutError:
                return 0
            if not line:
                return 0
            try:
                response = await client.post(
                    url,
                    content=line,
                    headers={
                        "content-type": "application/json",
                        "accept": "application/json, text/event-stream",
                    },
                )
            except httpx.HTTPError as exc:
                # Reported on stderr, never on stdout: stdout carries framed
                # JSON-RPC, and one diagnostic line there desynchronises the
                # client for the rest of the session.
                print(f"bridge: {exc}", file=sys.stderr, flush=True)
                continue
            body = response.text.strip()
            if body:
                sys.stdout.write(body + "\n")
                sys.stdout.flush()


def run(url: str = "", idle_seconds: float = 0) -> int:
    return asyncio.run(_pump(url or config.MCP_URL, idle_seconds or config.BRIDGE_IDLE_S))
