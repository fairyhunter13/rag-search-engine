#!/usr/bin/env python3
"""Deterministic MCP stdio harness (no LLM) for opencode-search.

This spawns the stdio bridge (`opencode-search daemon bridge-stdio`) and speaks
MCP over stdio using the official Python SDK.

Goal: catch retrieval/rerank regressions that historically showed up only when
used via Claude Code/Codex.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _result_to_dict(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured

    content = getattr(result, "content", None) or []
    if len(content) == 1 and getattr(content[0], "type", None) == "text":
        text = getattr(content[0], "text", "")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"result": text}
        return parsed if isinstance(parsed, dict) else {"result": parsed}

    return {"status": "error", "error": "Unexpected tool result format"}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _idx(paths: list[str], suffix: str) -> int | None:
    for i, p in enumerate(paths):
        if p.endswith(suffix):
            return i
    return None


async def _run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="opencode-search-mcp-harness-"))
    workspace = tmp / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # Isolate registry + daemon state so this harness cannot interfere with any
    # user daemon/indexes.
    registry_path = tmp / "registry.json"
    state_dir = tmp / "daemon-state"
    port = _free_tcp_port()

    # Project A: stale docs vs source-of-truth regression.
    proj_a = workspace / "proj-a"
    _write(
        proj_a / "src" / "config.py",
        "REGISTRY_PATH = '~/.local/share/opencode-search/projects.json'\\n",
    )
    _write(
        proj_a / "docs" / "MIGRATION_PLAN.md",
        "Registry path is ~/.opencode/projects.json in the legacy design.\\n",
    )
    _write(
        proj_a / "scripts" / "benchmark_mcp.py",
        'QUESTIONS = [\"Where is the registry of indexed projects stored and what format is it?\"]\\n',
    )

    # Project B + C: federated search check.
    proj_b = workspace / "proj-b"
    _write(
        proj_b / "src" / "alpha.py",
        "FED_ALPHA = 'federated_alpha_unique'\\n",
    )
    proj_c = workspace / "proj-c"
    _write(
        proj_c / "src" / "beta.py",
        "FED_BETA = 'federated_beta_unique'\\n",
    )

    env = {
        # Keep all state in tmp.
        "OPENCODE_REGISTRY_PATH": str(registry_path),
        "OPENCODE_MCP_STATE_DIR": str(state_dir),
        "OPENCODE_MCP_DAEMON_HOST": "127.0.0.1",
        "OPENCODE_MCP_DAEMON_PORT": str(port),
        # Ensure workspace scoping is active.
        "OPENCODE_BRIDGE_WORKSPACE_ROOT": str(workspace),
        # Keep the daemon from lingering too long after the harness exits.
        "OPENCODE_MCP_IDLE_SHUTDOWN_S": "30",
    }

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "opencode_search", "daemon", "bridge-stdio"],
        env={**os.environ, **env},
        cwd=str(workspace),
    )

    async with stdio_client(params) as streams:
        read_stream, write_stream = streams
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            tools = await session.list_tools()
            tool_names = {t.name for t in getattr(tools, "tools", [])}
            required = {
                "index_project",
                "search_code",
                "project_status",
                "list_indexed_projects",
                "stop_watching",
            }
            missing = required - tool_names
            if missing:
                print(f"ERROR: missing tools: {sorted(missing)}", file=sys.stderr)
                return 2

            # Index all projects (budget tier for speed).
            for proj in (proj_a, proj_b, proj_c):
                res = await session.call_tool(
                    "index_project",
                    {"path": str(proj), "tier": "budget", "watch": False, "force": True},
                )
                d = _result_to_dict(res)
                if d.get("status") != "ok":
                    print(f"ERROR: index_project failed for {proj}: {d}", file=sys.stderr)
                    return 3

            # Assert stale docs/tests/benchmarks do not outrank implementation.
            query = "Where is the registry of indexed projects stored and what format is it?"
            res = await session.call_tool(
                "search_code",
                {
                    "query": query,
                    "project_paths": [str(proj_a)],
                    "top_k": 8,
                    "use_rerank": True,
                },
            )
            d = _result_to_dict(res)
            rows = d.get("results", [])
            if not rows:
                print(f"ERROR: search_code returned no results for query={query!r}", file=sys.stderr)
                return 4
            paths = [r.get("path", "") for r in rows]
            cfg_i = _idx(paths, "src/config.py")
            doc_i = _idx(paths, "docs/MIGRATION_PLAN.md")
            bench_i = _idx(paths, "scripts/benchmark_mcp.py")
            if cfg_i is None:
                print(f"ERROR: expected src/config.py in results; got paths={paths}", file=sys.stderr)
                return 5
            if doc_i is not None and not (cfg_i < doc_i):
                print(f"ERROR: stale docs outranked config. paths={paths}", file=sys.stderr)
                return 6
            if bench_i is not None and not (cfg_i < bench_i):
                print(f"ERROR: benchmark question text outranked config. paths={paths}", file=sys.stderr)
                return 7

            # Federated search: ensure results return from multiple projects.
            res = await session.call_tool(
                "search_code",
                {
                    "query": "federated_alpha_unique federated_beta_unique",
                    "project_paths": [str(proj_b), str(proj_c)],
                    "top_k": 6,
                    "use_rerank": True,
                },
            )
            d = _result_to_dict(res)
            rows = d.get("results", [])
            contents = " ".join(str(r.get("content", "")) for r in rows)
            if "federated_alpha_unique" not in contents or "federated_beta_unique" not in contents:
                print(f"ERROR: federated search missing expected tokens. results={rows}", file=sys.stderr)
                return 8

    print("OK: MCP stdio harness passed.")
    return 0


def main() -> None:
    raise SystemExit(anyio.run(_run))


if __name__ == "__main__":
    main()

