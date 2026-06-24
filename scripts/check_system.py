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
    "opencode_search.server.mcp",
    "opencode_search.core.config",
    "opencode_search.core.registry",
    "opencode_search.core.gpu",
    "opencode_search.embed",
    "opencode_search.index.store",
    "opencode_search.graph.store",
    "opencode_search.kb.hierarchy",
    "opencode_search.kb.wiki",
    "opencode_search.query.search",
    "opencode_search.daemon",
    "opencode_search.cli",
    "opencode_search.server._overview",
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
    "EMBED_MODEL": "jinaai/jina-embeddings-v2-base-code",
    "RERANK_MODEL": "jinaai/jina-reranker-v1-turbo-en",
    "QUERY_LLM_MODEL": "claude-haiku-4-5",
}


def check_config() -> None:
    print("\n### Config constants")
    try:
        from opencode_search.core import config
    except ImportError as exc:
        _fail("opencode_search.core.config importable", str(exc))
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
        from opencode_search.core.gpu import assert_gpu_available
        assert_gpu_available()
        _ok("assert_gpu_available() — GPU EP present")
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

EXPECTED_MCP_TOOLS = {"search", "ask", "graph", "overview", "index"}


def check_mcp_tools() -> None:
    print("\n### MCP tools (server/mcp.py)")
    try:
        from opencode_search.server.mcp import mcp as _mcp
        tool_names = set(t.name for t in asyncio.run(_mcp.list_tools()))
        if tool_names == EXPECTED_MCP_TOOLS:
            _ok(f"Exactly {len(EXPECTED_MCP_TOOLS)} tools registered: {sorted(EXPECTED_MCP_TOOLS)}")
        else:
            for t in sorted(EXPECTED_MCP_TOOLS - tool_names):
                _fail(f"  tool missing: {t}")
            for t in sorted(tool_names - EXPECTED_MCP_TOOLS):
                _warn(f"  unexpected tool: {t}")
    except Exception as exc:
        _fail("server.mcp importable and tools listable", str(exc))


# ---------------------------------------------------------------------------
# Section: CLI commands
# ---------------------------------------------------------------------------

CLI_COMMANDS = ["opencode-search"]

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
    print("\n### LLM provider (GPU = embed+rerank only; chat = claude-haiku-4-5 only; DeepSeek = KB enrichment only)")
    try:
        from opencode_search.core.config import QUERY_LLM_MODEL
    except ImportError as exc:
        _fail("core.config importable", str(exc))
        return
    _ok(f"QUERY_LLM_MODEL (chat, haiku-only) = {QUERY_LLM_MODEL}")
    # DeepSeek = KB-enrichment-exclusive; not a chat fallback (HR12)
    try:
        from opencode_search.graph.llm import deepseek_key
        key = deepseek_key()
        if key:
            _ok("DEEPSEEK_API_KEY found — KB enrichment available (KB-exclusive; no chat fallback)")
        else:
            _fail("DEEPSEEK_API_KEY", "not found in env or ~/.bash_env — KB build will crash", required=False)
    except Exception as exc:
        _fail("deepseek_key()", str(exc), required=False)
    # claude CLI is the sole chat lane; no DeepSeek fallback (F / HR10)
    import shutil
    claude = shutil.which("claude")
    if claude:
        _ok(f"claude CLI found at {claude} — haiku-4-5 chat lane active")
    else:
        _warn("claude CLI not found — chat will emit SSE error (no DeepSeek fallback; KB/search unaffected)")


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
