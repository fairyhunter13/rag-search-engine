"""MCP stdio harness — deterministic end-to-end test of the stdio bridge.

Spawns `opencode-search daemon bridge-stdio`, speaks MCP over stdio using
the official Python SDK, indexes synthetic projects, and asserts:
  - All 7 current tools are registered (search, ask, graph, overview, build, federation, manage)
  - Excluded files (docs/, scripts/) are NOT indexed
  - Source code outranks stale docs for question-like queries
  - Federated search returns results from multiple projects
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("anyio")
pytest.importorskip("mcp")


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _result_to_dict(result) -> dict:
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


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.runtime_deps
async def test_mcp_stdio_harness():
    """Full MCP stdio bridge: tool registration, search ranking, federated search."""
    import anyio
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    tmp = Path(tempfile.mkdtemp(prefix="opencode-search-mcp-harness-"))
    workspace = tmp / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    repo_root = Path(__file__).resolve().parent.parent.parent.parent

    registry_path = tmp / "registry.json"
    state_dir = tmp / "daemon-state"
    port = _free_tcp_port()

    # Project A: source code vs stale docs vs excluded scripts
    proj_a = workspace / "proj-a"
    _write(proj_a / ".opencode-index.yaml",
           "index:\n  exclude:\n    - \"docs/**\"\n    - \"scripts/**\"\n")
    _write(proj_a / "src" / "config.py",
           "REGISTRY_PATH = '~/.local/share/opencode-search/projects.json'\n")
    _write(proj_a / "docs" / "MIGRATION_PLAN.md",
           "Registry path is ~/.opencode/projects.json in the legacy design.\n")
    _write(proj_a / "scripts" / "benchmark_mcp.py",
           'QUESTIONS = ["Where is the registry stored?"]\n')

    # Projects B + C: federated search
    proj_b = workspace / "proj-b"
    _write(proj_b / "src" / "alpha.py", "FED_ALPHA = 'federated_alpha_unique'\n")
    proj_c = workspace / "proj-c"
    _write(proj_c / "src" / "beta.py", "FED_BETA = 'federated_beta_unique'\n")

    env = {
        "OPENCODE_REGISTRY_PATH": str(registry_path),
        "OPENCODE_MCP_STATE_DIR": str(state_dir),
        "OPENCODE_MCP_DAEMON_HOST": "127.0.0.1",
        "OPENCODE_MCP_DAEMON_PORT": str(port),
        "PYTHONPATH": str(repo_root / "src"),
        "OPENCODE_BRIDGE_WORKSPACE_ROOT": str(workspace),
        "OPENCODE_MCP_IDLE_SHUTDOWN_S": "30",
        "OPENCODE_WEIGHT_SRC": "2.0",
        "OPENCODE_WEIGHT_DOCS": "0.1",
        "OPENCODE_WEIGHT_SCRIPTS": "0.1",
        "OPENCODE_WEIGHT_DOCUMENT_LANGUAGE": "0.1",
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
            # Current 7-tool API (v2, June 2026)
            required = {"search", "ask", "graph", "overview", "build", "federation", "manage"}
            missing = required - tool_names
            assert not missing, f"Missing MCP tools: {sorted(missing)}"

            # Index all three projects using build(action="index") for fast indexing.
            # The call returns immediately with status="indexing" — wait for completion.
            for proj in (proj_a, proj_b, proj_c):
                res = await session.call_tool(
                    "build",
                    {"project_path": str(proj), "action": "index", "watch": False, "force": True},
                )
                d = _result_to_dict(res)
                assert d.get("status") in ("ok", "indexing"), (
                    f"build(index) failed for {proj}: {d}"
                )

            # Wait for background indexing to drain across all 3 projects
            await anyio.sleep(15)

            # Source code must outrank excluded/stale docs
            query = "Where is the registry of indexed projects stored and what format is it?"
            res = await session.call_tool(
                "search",
                {"query": query, "project_paths": [str(proj_a)], "top_k": 8},
            )
            d = _result_to_dict(res)
            rows = d.get("results", [])
            assert rows, f"search returned no results for: {query!r}"
            paths = [r.get("path", "") for r in rows]
            cfg_i = _idx(paths, "src/config.py")
            doc_i = _idx(paths, "docs/MIGRATION_PLAN.md")
            bench_i = _idx(paths, "scripts/benchmark_mcp.py")
            assert cfg_i is not None, f"src/config.py not in results: {paths}"
            assert doc_i is None, f"Excluded docs were indexed: {paths}"
            assert bench_i is None, f"Excluded scripts were indexed: {paths}"

            # Federated search must return results from both proj_b and proj_c
            res = await session.call_tool(
                "search",
                {"query": "federated_alpha_unique federated_beta_unique",
                 "project_paths": [str(proj_b), str(proj_c)], "top_k": 6},
            )
            d = _result_to_dict(res)
            rows = d.get("results", [])
            contents = " ".join(str(r.get("content", "")) for r in rows)
            assert "federated_alpha_unique" in contents, f"federated alpha missing: {rows}"
            assert "federated_beta_unique" in contents, f"federated beta missing: {rows}"
