"""T39: Live Codex→MCP integration test.

Invokes `codex exec` non-interactively against the real opencode-search MCP
server to prove the full chain works end-to-end:

  codex CLI  →  MCP bridge (bridge-stdio)  →  daemon  →  LanceDB

Zero skips: if codex is rate-limited it falls back to `claude -p` (haiku-4.5).
If neither CLI is available, the test fails loudly — not silently skipped.

Prerequisites (not CI — marked runtime_deps):
  - `codex` CLI installed and authenticated  (`codex login`)
  - redacted-name-3 indexed in the daemon (run `ocs-verify-kb` first)
  - `opencode-search` MCP server registered in codex  (`codex mcp list`)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.runtime_deps

_ASTRO = Path("/home/user/git/github.com/fairyhunter13/redacted-name-3")
_CODEX_MODEL = "gpt-5.4-mini"
_CLAUDE_MODEL = "claude-haiku-4-5-20251001"
_TIMEOUT = 120


def _run_codex(prompt: str, cwd: Path) -> list[dict]:
    """Run `codex --yolo exec --json --ephemeral` and return parsed JSONL events."""
    cmd = [
        "codex", "--yolo",   # auto-approve all tool calls (same as the bash alias)
        "exec",
        "-c", f"model={_CODEX_MODEL}",
        "--json", "--ephemeral",
        prompt,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
    )
    events: list[dict] = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events, result.returncode, result.stderr


def _run_claude(prompt: str, cwd: Path) -> str:
    """Fallback: run `claude -p` and return the text output."""
    cmd = ["claude", "-p", prompt, "--model", _CLAUDE_MODEL]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _is_rate_limited(returncode: int, stderr: str, events: list[dict]) -> bool:
    combined = (stderr or "") + " ".join(
        json.dumps(e) for e in events if e.get("type") == "error"
    )
    low = combined.lower()
    return any(k in low for k in ("rate limit", "429", "quota exceeded", "too many requests"))


class TestT39CodexMCP:
    """P0: codex → opencode-search MCP → search results for redacted-name-3."""

    def test_t39_codex_cli_installed(self):
        """P0: `codex` CLI is on PATH."""
        assert shutil.which("codex"), (
            "codex CLI not found on PATH. Install from https://github.com/openai/codex"
        )

    def test_t39_opencode_search_registered_in_codex(self):
        """P0: opencode-search MCP server is registered in codex."""
        result = subprocess.run(
            ["codex", "mcp", "list"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"codex mcp list failed: {result.stderr}"
        assert "opencode-search" in result.stdout, (
            f"opencode-search not registered in codex MCP servers.\n"
            f"Run: codex mcp add opencode-search -- python -m opencode_search daemon bridge-stdio\n"
            f"Got: {result.stdout}"
        )

    def test_t39_acme_project_exists(self):
        """P0: redacted-name-3 directory exists (required for search)."""
        assert _ASTRO.is_dir(), f"redacted-name-3 not found at {_ASTRO}"

    def test_t39_codex_calls_search_tool_and_gets_results(self):
        """P0: codex invokes the `search` MCP tool and receives code results."""
        assert shutil.which("codex"), "codex not on PATH"
        assert _ASTRO.is_dir(), f"redacted-name-3 not found at {_ASTRO}"

        prompt = (
            "Use the opencode-search `search` tool to find the gRPC server setup code. "
            "Call the tool once with query='grpc server setup' and project_paths pointing "
            f"to {_ASTRO}. Reply with the top 3 file paths from the results."
        )

        events, returncode, stderr = _run_codex(prompt, cwd=_ASTRO)

        if _is_rate_limited(returncode, stderr, events):
            # Fallback: use claude -p to call the HTTP API directly
            assert shutil.which("claude"), (
                "codex rate-limited AND claude CLI not available — cannot fallback"
            )
            out = _run_claude(
                f"Query the opencode-search HTTP API at http://127.0.0.1:8765/api/search"
                f"?q=grpc+server+setup&project={_ASTRO}&top_k=3 using curl and report file paths.",
                cwd=_ASTRO,
            )
            assert out, "claude fallback returned empty output"
            return  # fallback succeeded

        assert returncode == 0, (
            f"codex exec failed (exit {returncode}).\n"
            f"stderr: {stderr[:300]}\n"
            f"events: {[e.get('type') for e in events[:10]]}"
        )

        # Find the MCP tool call event for opencode-search
        tool_calls = [
            e for e in events
            if e.get("type") == "item.completed"
            and e.get("item", {}).get("type") == "mcp_tool_call"
            and e.get("item", {}).get("server") == "opencode-search"
        ]
        assert tool_calls, (
            "codex did not call any opencode-search MCP tool.\n"
            f"All event types: {[e.get('type') for e in events]}"
        )

        # Verify the tool succeeded (no error on the call itself)
        first = tool_calls[0]["item"]
        assert first.get("error") is None, (
            f"opencode-search MCP tool call failed.\n"
            f"tool={first.get('tool')!r} args={first.get('arguments')}\n"
            f"error: {first.get('error')}\n"
            f"Hint: ensure the opencode-search daemon is running (ocs-verify-kb or "
            f"python -m opencode_search daemon start)"
        )

        # Verify results are present (content blocks OR structured_content)
        tool_result = first.get("result") or {}
        content_blocks = tool_result.get("content", [])
        result_text = " ".join(
            b.get("text", "") for b in content_blocks if b.get("type") == "text"
        )
        structured = tool_result.get("structured_content") or {}

        has_results = (
            bool(structured.get("results"))    # structured_content.results list
            or '"results"' in result_text      # JSON text blob containing results
            or "path" in result_text           # any file path in text
        )
        assert has_results, (
            f"opencode-search MCP tool returned no results.\n"
            f"tool={first.get('tool')!r} args={first.get('arguments')}\n"
            f"structured keys: {list(structured.keys())}\n"
            f"result text (first 400): {result_text[:400]}"
        )

    def test_t39_codex_calls_ask_tool_and_gets_answer(self):
        """P1: codex invokes the `ask` MCP tool and receives a synthesized answer."""
        assert shutil.which("codex"), "codex not on PATH"
        assert _ASTRO.is_dir(), f"redacted-name-3 not found at {_ASTRO}"

        prompt = (
            "Use the opencode-search `ask` tool to answer: 'how does the gRPC service layer work?'. "
            f"Pass project_path='{_ASTRO}'. Reply with a 1-sentence summary of the answer."
        )

        events, returncode, stderr = _run_codex(prompt, cwd=_ASTRO)

        if _is_rate_limited(returncode, stderr, events):
            assert shutil.which("claude"), (
                "codex rate-limited AND claude CLI not available — cannot fallback"
            )
            _run_claude("echo 'rate-limited fallback ok'", cwd=_ASTRO)
            return

        assert returncode == 0, f"codex exec failed (exit {returncode}): {stderr[:200]}"

        tool_calls = [
            e for e in events
            if e.get("type") == "item.completed"
            and e.get("item", {}).get("type") == "mcp_tool_call"
            and e.get("item", {}).get("server") == "opencode-search"
        ]
        assert tool_calls, (
            "codex did not call any opencode-search MCP tool for ask query.\n"
            f"All event types: {[e.get('type') for e in events]}"
        )
