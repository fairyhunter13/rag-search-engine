#!/usr/bin/env python3
"""opencode-search system health + behaviour checklist.

Usage:
    python scripts/check_system.py            # print to stdout
    python scripts/check_system.py CHECKLIST.md   # also write to a file

Exits 0 if all *required* checks pass, 1 if any required check fails.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import shutil
import sys
import urllib.request
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PASS = "[x]"
FAIL = "[ ]"
WARN = "[~]"  # informational / optional

_results: list[tuple[bool, str, str]] = []  # (required, mark, message)


def _ok(msg: str, *, required: bool = True) -> None:
    _results.append((required, PASS, msg))
    print(f"  {PASS} {msg}")


def _fail(msg: str, detail: str = "", *, required: bool = True) -> None:
    suffix = f" ({detail})" if detail else ""
    _results.append((required, FAIL, msg + suffix))
    print(f"  {FAIL} {msg}{suffix}")


def _warn(msg: str, detail: str = "") -> None:
    suffix = f" ({detail})" if detail else ""
    _results.append((False, WARN, msg + suffix))
    print(f"  {WARN} {msg}{suffix}")


# ---------------------------------------------------------------------------
# Section: Core imports
# ---------------------------------------------------------------------------

CORE_MODULES = [
    "opencode_search.mcp",
    "opencode_search.mcp_bridge",
    "opencode_search.embeddings",
    "opencode_search.config",
    "opencode_search.daemon",
    "opencode_search.indexer",
    "opencode_search.search",
    "opencode_search.storage",
    "opencode_search.cli",
    "opencode_search.handlers",
    "opencode_search.enricher.client",
    "opencode_search.graph",
    "opencode_search.wiki",
    "opencode_search.metrics",
    "opencode_search.watcher",
    "opencode_search.chunker",
    "opencode_search.discover",
]


def check_imports() -> None:
    print("\n### Core imports")
    for mod in CORE_MODULES:
        try:
            importlib.import_module(mod)
            _ok(f"import {mod}")
        except ImportError as exc:
            _fail(f"import {mod}", str(exc))


# ---------------------------------------------------------------------------
# Section: Config constants
# ---------------------------------------------------------------------------

EXPECTED_CONFIG: dict[str, str] = {
    "DEFAULT_EMBED_MODEL": "jinaai/jina-embeddings-v2-base-code",
    "DEFAULT_RERANK_MODEL": "jinaai/jina-reranker-v1-turbo-en",
    "DEFAULT_LLM_PROVIDER": "ollama",
    "DEFAULT_LLM_MODEL": "phi4-mini:3.8b",
}


def check_config() -> None:
    print("\n### Config constants")
    try:
        from opencode_search import config
    except ImportError as exc:
        _fail("opencode_search.config importable", str(exc))
        return

    for name, default_val in EXPECTED_CONFIG.items():
        actual = getattr(config, name, None)
        if actual is None:
            _fail(f"Config: {name} exists")
        else:
            # Show actual value (may be overridden by env vars)
            _ok(f"Config: {name} = {actual}")
            if actual != default_val:
                _warn(f"  (default is {default_val!r} but env override active)")


# ---------------------------------------------------------------------------
# Section: GPU
# ---------------------------------------------------------------------------


def check_gpu() -> None:
    print("\n### GPU")
    try:
        from opencode_search.embeddings import assert_gpu_available
        assert_gpu_available()
        _ok("assert_gpu_available() — CUDA provider present")
    except SystemExit as exc:
        _fail("assert_gpu_available()", f"SystemExit({exc.code})")
    except Exception as exc:
        _fail("assert_gpu_available()", str(exc))


# ---------------------------------------------------------------------------
# Section: Daemon
# ---------------------------------------------------------------------------

DAEMON_URL = os.environ.get("OPENCODE_DAEMON_URL", "http://127.0.0.1:8765")


def check_daemon() -> None:
    print("\n### HTTP Daemon")
    healthz = f"{DAEMON_URL}/healthz"
    try:
        with urllib.request.urlopen(healthz, timeout=3) as resp:
            import json
            body = json.loads(resp.read().decode("utf-8"))
            _ok(f"Daemon reachable at {DAEMON_URL} (ok={body.get('ok')})")
    except Exception as exc:
        _fail(f"Daemon reachable at {DAEMON_URL}", str(exc), required=False)


# ---------------------------------------------------------------------------
# Section: MCP tools registered
# ---------------------------------------------------------------------------

EXPECTED_MCP_TOOLS = [
    "search_code",
    "index_project",
    "project_status",
    "list_indexed_projects",
    "stop_watching",
    "search_metrics",
    "get_symbol",
    "get_callers",
    "get_callees",
    "trace_path",
    "detect_impact",
    "get_communities",
    "global_search",
    "enrich_project",
    "get_symbol_intent",
    "wiki_generate",
    "wiki_ingest",
    "wiki_query",
    "wiki_lint",
    "search_docs",
]

EXPECTED_BRIDGE_TOOLS = [
    "search_code",
    "index_project",
    "get_symbol",
    "get_callers",
    "get_callees",
    "trace_path",
    "detect_impact",
    "get_communities",
    "global_search",
    "enrich_project",
    "get_symbol_intent",
    "wiki_generate",
    "wiki_ingest",
    "wiki_query",
    "wiki_lint",
    "search_docs",
]


async def _list_tool_names_async(server_obj: object) -> list[str]:
    tools = await server_obj.list_tools()  # type: ignore[attr-defined]
    return [t.name for t in tools]


def check_mcp_tools() -> None:
    print("\n### MCP tools (mcp.py)")
    try:
        import opencode_search.mcp as mcp_mod
        tool_names = set(asyncio.run(_list_tool_names_async(mcp_mod.mcp)))
        _ok(f"{len(tool_names)} tools registered in mcp.py")
        for tool in EXPECTED_MCP_TOOLS:
            if tool in tool_names:
                _ok(f"  {tool}")
            else:
                _fail(f"  {tool}", "missing from mcp.py")
    except Exception as exc:
        _fail("opencode_search.mcp importable and tools listable", str(exc))


def check_bridge_tools() -> None:
    print("\n### MCP bridge tools (mcp_bridge.py)")
    try:
        import opencode_search.mcp_bridge as bridge_mod
        tool_names = set(asyncio.run(_list_tool_names_async(bridge_mod.bridge)))
        _ok(f"{len(tool_names)} tools registered in mcp_bridge.py")
        for tool in EXPECTED_BRIDGE_TOOLS:
            if tool in tool_names:
                _ok(f"  {tool}")
            else:
                _fail(f"  {tool}", "missing from mcp_bridge.py")
    except Exception as exc:
        _fail("opencode_search.mcp_bridge importable and tools listable", str(exc))


# ---------------------------------------------------------------------------
# Section: Handler modules
# ---------------------------------------------------------------------------

HANDLER_MODULES = [
    "opencode_search.handlers._common",
    "opencode_search.handlers._index",
    "opencode_search.handlers._query",
    "opencode_search.handlers._watch",
    "opencode_search.handlers._graph",
    "opencode_search.handlers._enrichment",
    "opencode_search.handlers._wiki",
]


def check_handler_modules() -> None:
    print("\n### Handler modules")
    for mod in HANDLER_MODULES:
        try:
            importlib.import_module(mod)
            _ok(f"import {mod}")
        except ImportError as exc:
            _fail(f"import {mod}", str(exc))


# ---------------------------------------------------------------------------
# Section: CLI commands
# ---------------------------------------------------------------------------

CLI_COMMANDS = [
    "opencode-search",
    "opencode-search-init",
]

VENV_BIN = Path(__file__).resolve().parent.parent / ".venv" / "bin"


def check_cli() -> None:
    print("\n### CLI commands")
    for cmd in CLI_COMMANDS:
        # Check PATH first, then .venv/bin
        found = shutil.which(cmd)
        if found:
            _ok(f"{cmd} found at {found}")
        elif (VENV_BIN / cmd).exists():
            _ok(f"{cmd} found in .venv/bin (not on PATH — add .venv/bin to PATH)")
        else:
            _fail(f"{cmd} found on PATH or .venv/bin", required=False)


# ---------------------------------------------------------------------------
# Section: LLM provider
# ---------------------------------------------------------------------------


def check_llm_provider() -> None:
    print("\n### LLM provider")
    try:
        from opencode_search.config import DEFAULT_LLM_PROVIDER, DEFAULT_LLM_MODEL
        from opencode_search.enricher.client import (
            ClaudeCodeClient,
            CodexClient,
            OllamaClient,
        )
    except ImportError as exc:
        _fail("enricher.client importable", str(exc))
        return

    provider = DEFAULT_LLM_PROVIDER.lower()
    _ok(f"OPENCODE_LLM_PROVIDER = {provider}")
    _ok(f"OPENCODE_LLM_MODEL    = {DEFAULT_LLM_MODEL}")

    if provider == "none":
        _warn("LLM enrichment disabled (OPENCODE_LLM_PROVIDER=none)")
        return

    if provider == "ollama":
        base_url = os.environ.get("OPENCODE_LLM_BASE_URL", "http://localhost:11434")
        client = OllamaClient(base_url=base_url, model=DEFAULT_LLM_MODEL)
        if client.is_available():
            _ok(f"Ollama reachable at {base_url}")
        else:
            _fail(f"Ollama reachable at {base_url}", required=False)

    elif provider == "anthropic":
        api_key = os.environ.get("OPENCODE_LLM_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            _ok("ANTHROPIC_API_KEY set")
        else:
            _fail("ANTHROPIC_API_KEY set (required for anthropic provider)", required=False)

    elif provider == "openai":
        api_key = os.environ.get("OPENCODE_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        if api_key:
            _ok("OPENAI_API_KEY set")
        else:
            _fail("OPENAI_API_KEY set (required for openai provider)", required=False)

    elif provider == "claude-code":
        client = ClaudeCodeClient()
        if client.is_available():
            _ok(f"claude CLI found at {shutil.which('claude')}")
        else:
            _fail("claude CLI found on PATH", required=False)

    elif provider == "codex":
        client = CodexClient()
        if client.is_available():
            _ok(f"codex CLI found at {shutil.which('codex')}")
        else:
            _fail("codex CLI found on PATH", required=False)

    else:
        _warn(f"Unknown provider {provider!r} — no availability check performed")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _render_markdown(output_file: str | None = None) -> str:
    lines: list[str] = [
        "## opencode-search System Checklist",
        f"Generated: {date.today()}",
        "",
    ]

    # Aggregate counts
    passed = sum(1 for _, mark, _ in _results if mark == PASS)
    failed = sum(1 for req, mark, _ in _results if mark == FAIL and req)
    warnings = sum(1 for _, mark, _ in _results if mark == WARN)
    optional_fail = sum(1 for req, mark, _ in _results if mark == FAIL and not req)

    for _, mark, msg in _results:
        lines.append(f"- {mark} {msg}")

    lines.append("")
    lines.append(
        f"**Summary:** {passed} passed, {failed} required failures, "
        f"{optional_fail} optional failures, {warnings} warnings"
    )
    text = "\n".join(lines)

    if output_file:
        Path(output_file).write_text(text, encoding="utf-8")
        print(f"\nChecklist written to {output_file}")

    return text


def main() -> int:
    output_file = sys.argv[1] if len(sys.argv) > 1 else None

    # Ensure the src/ directory is on sys.path so the package is importable
    # whether the package is installed or run directly from the repo.
    src = str(Path(__file__).resolve().parent.parent / "src")
    if src not in sys.path:
        sys.path.insert(0, src)

    print("## opencode-search System Checklist")
    print(f"Generated: {date.today()}")

    check_imports()
    check_config()
    check_gpu()
    check_daemon()
    check_mcp_tools()
    check_bridge_tools()
    check_handler_modules()
    check_cli()
    check_llm_provider()

    print()
    _render_markdown(output_file)

    # Exit 1 if any *required* check failed
    failed_required = [msg for req, mark, msg in _results if mark == FAIL and req]
    if failed_required:
        print(f"\nFAILED: {len(failed_required)} required check(s) failed.")
        return 1
    print("\nAll required checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
