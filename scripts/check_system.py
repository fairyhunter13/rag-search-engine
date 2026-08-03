#!/usr/bin/env python3
"""rag-search system health + behaviour checklist.

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
    "rag_search.server.mcp",
    "rag_search.core.config",
    "rag_search.core.registry",
    "rag_search.core.gpu",
    "rag_search.embed",
    "rag_search.index.store",
    "rag_search.graph.store",
    # kb.wiki left with tier 3, and answer_cache — the one deterministic module the package still
    # held — moved to query/ on 2026-07-28, retiring `rag_search.kb` entirely. Re-pointed rather
    # than dropped: query/ask.py and server/routes_chat.py both import it, so it is still worth an
    # import check. Dropping the row silently would have been the same stale-allowlist hole R0 has
    # found repeatedly — this list has no exhaustiveness test, so each entry has to be re-pointed
    # or removed by hand.
    "rag_search.query.answer_cache",
    "rag_search.query.search",
    "rag_search.daemon",
    "rag_search.cli",
    "rag_search.server._overview",
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

# Must track core/config.py's defaults: a mismatch here is reported to the user as "env override
# active", so a stale entry accuses a clean machine of a misconfiguration it does not have.
# RERANK_MODEL read jinaai/jina-reranker-v1-turbo-en until 2026-07-31, four weeks after the
# measured A/B moved the default to gte-reranker-modernbert-base — every clean setup run warned.
# EMBED_MODEL repeated it in miniature on 2026-08-03: the nomic switch moved the default and this
# table is the one place that has to be edited by hand in the same commit.
EXPECTED_CONFIG: dict[str, str] = {
    "EMBED_MODEL": "nomic-ai/nomic-embed-text-v1.5",
    "RERANK_MODEL": "Alibaba-NLP/gte-reranker-modernbert-base",
    "QUERY_LLM_MODEL": "claude-haiku-4-5",
}


def check_config() -> None:
    print("\n### Config constants")
    try:
        from rag_search.core import config
    except ImportError as exc:
        _fail("rag_search.core.config importable", str(exc))
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
        from rag_search.core.gpu import assert_gpu_available
        assert_gpu_available()
        _ok("assert_gpu_available() — GPU EP present")
    except SystemExit as exc:
        _fail("assert_gpu_available()", f"SystemExit({exc.code})")
    except Exception as exc:
        _fail("assert_gpu_available()", str(exc))


# ---------------------------------------------------------------------------
# Section: Daemon
# ---------------------------------------------------------------------------

DAEMON_URL = os.environ.get("RSE_DAEMON_URL", "http://127.0.0.1:8765")


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

EXPECTED_MCP_TOOLS = {"search", "graph", "overview", "index"}


def check_mcp_tools() -> None:
    print("\n### MCP tools (server/mcp.py)")
    try:
        from rag_search.server.mcp import mcp as _mcp
        tool_names = {t.name for t in asyncio.run(_mcp.list_tools())}
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

CLI_COMMANDS = ["rag-search"]

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
    print("\n### LLM provider (GPU = embed+rerank only; chat = claude-haiku-4-5 only)")
    try:
        from rag_search.core.config import QUERY_LLM_MODEL
    except ImportError as exc:
        _fail("core.config importable", str(exc))
        return
    _ok(f"QUERY_LLM_MODEL (chat, haiku-only) = {QUERY_LLM_MODEL}")
    # The DEEPSEEK_API_KEY check that stood here died with tier 3, and what it gated
    # is now the opposite of a requirement: graph/llm.py was the only module in the
    # repo that opened an LLM URL, so a *missing* key is the normal configuration and
    # a reader of it would be a regression. That property is asserted where it can
    # actually fail — the tree-wide no-LLM-client guard in the live suite — rather
    # than by a health check that would report "all clear" either way.
    # claude CLI is the sole generative lane, and it is chat-only.
    import shutil
    claude = shutil.which("claude")
    if claude:
        _ok(f"claude CLI found at {claude} — haiku-4-5 chat lane active")
    else:
        _warn("claude CLI not found — chat will emit SSE error (search/graph unaffected)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _render_markdown(output_file: str | None = None) -> str:
    lines: list[str] = [
        "## rag-search System Checklist",
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

    print("## rag-search System Checklist")
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
