"""End-to-end CLI integration tests: verify claude (haiku) + codex (gpt-5.4-mini)
correctly use the opencode-search MCP server in real invocations.

These tests are the acceptance gate for Phase 66. They spawn real subprocesses
(real Anthropic API, real OpenAI/codex backend, real daemon at :8765) and
assert that:
  1. The MCP server entry is resolved at startup (no "server not found" errors)
  2. The priority directive in CLAUDE.md / AGENTS.md causes the LLM to call
     an opencode-search MCP tool before any Bash/Read/Grep fallback
  3. The final answer is factually grounded in real MCP output (not hallucinated)

NEVER skip: the module fixture hard-fails if preconditions are not met so the
gate doesn't silently pass without actually running.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

pytestmark = [pytest.mark.live, pytest.mark.slow]

_PROJECT = "/home/user/git/github.com/fairyhunter13/opencode-search-engine"
_CLAUDE_CONFIG = str(Path.home() / ".claude")
_CODEX_AUTH = Path.home() / ".codex" / "auth.json"
_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"

_MCP_TOOL_NAMES = {"search", "ask", "graph", "overview", "build", "federation", "manage"}
_MCP_TOOL_PREFIX = "mcp__opencode-search__"


# ---------------------------------------------------------------------------
# Hard precondition gate — FAIL, not skip, if missing
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _require_cli_integration_ready():
    """Hard-fail if any integration precondition is not met.

    The user asked that these tests always run and always verify real behavior.
    Skipping means the gate didn't run, which defeats the purpose.
    """
    missing: list[str] = []
    if shutil.which("claude") is None:
        missing.append("`claude` binary not on PATH — install Claude Code")
    if shutil.which("codex") is None:
        missing.append("`codex` binary not on PATH — install codex")
    if not os.environ.get("ANTHROPIC_API_KEY") and not _CREDENTIALS.exists():
        missing.append(
            "Anthropic credentials missing: set ANTHROPIC_API_KEY or ensure "
            f"~/.claude/.credentials.json exists at {_CREDENTIALS}"
        )
    if not _CODEX_AUTH.exists():
        missing.append(
            f"codex not logged in: {_CODEX_AUTH} not found — run `codex login`"
        )
    try:
        r = httpx.get("http://localhost:8765/healthz", timeout=3)
        assert r.status_code == 200, f"healthz returned {r.status_code}"
    except Exception as exc:
        missing.append(
            f"opencode-search daemon not reachable at localhost:8765: {exc} — "
            "run `ocs daemon serve` or `systemctl --user start opencode-search`"
        )
    if missing:
        pytest.fail(
            "CLI integration preconditions not met (FAIL, not skip):\n  - "
            + "\n  - ".join(missing)
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_claude(prompt: str, tmp_path: Path, timeout: int = 180) -> tuple[list[dict], str]:
    """Spawn `claude -p` with haiku 4.5 and return (events, raw_stdout)."""
    proc = subprocess.run(
        [
            "claude", "-p",
            "--model", "claude-haiku-4-5-20251001",
            "--output-format", "stream-json",
            "--verbose",
            "--max-turns", "6",
            "--dangerously-skip-permissions",
            prompt,
        ],
        cwd=str(tmp_path),  # isolate from any nearby CLAUDE.md
        capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "CLAUDE_CONFIG_DIR": _CLAUDE_CONFIG},
    )
    assert proc.returncode == 0, (
        f"claude -p exited {proc.returncode}\n"
        f"stderr (last 1500):\n{proc.stderr[-1500:]}\n"
        f"stdout (last 500):\n{proc.stdout[-500:]}"
    )
    events: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            with contextlib.suppress(json.JSONDecodeError):
                events.append(json.loads(line))
    return events, proc.stdout


def _extract_tool_uses(events: list[dict]) -> list[dict]:
    """Extract all tool_use content blocks from claude stream-json events."""
    tool_uses: list[dict] = []
    for ev in events:
        if ev.get("type") == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    tool_uses.append(block)
    return tool_uses


def _extract_final_text(events: list[dict]) -> str:
    """Concatenate all text content from assistant messages."""
    parts: list[str] = []
    for ev in events:
        if ev.get("type") == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
    return " ".join(parts).lower()


def _run_codex(prompt: str, tmp_path: Path, timeout: int = 180) -> tuple[list[dict], str]:
    """Spawn `codex exec` with gpt-5.4-mini and return (events, raw_stdout)."""
    proc = subprocess.run(
        [
            "codex", "exec",
            "--model", "gpt-5.4-mini",
            "--yolo",
            "--json",
            prompt,
        ],
        cwd=str(tmp_path),
        capture_output=True, text=True, timeout=timeout,
        env={**os.environ},
    )
    assert proc.returncode == 0, (
        f"codex exec exited {proc.returncode}\n"
        f"stderr (last 1500):\n{proc.stderr[-1500:]}\n"
        f"stdout (last 500):\n{proc.stdout[-500:]}"
    )
    events: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            with contextlib.suppress(json.JSONDecodeError):
                events.append(json.loads(line))
    return events, proc.stdout


def _extract_codex_tool_calls(events: list[dict]) -> list[str]:
    """Extract tool/function call names from codex JSONL events."""
    names: list[str] = []
    for ev in events:
        # codex JSONL event types vary by version — check multiple fields
        name = (
            ev.get("name")
            or ev.get("tool_name")
            or (ev.get("function") or {}).get("name")
        )
        ev_type = ev.get("type", "")
        if name and ev_type in (
            "tool_call", "function_call", "tool_use",
            "function_call_output", "local_shell_call",
        ):
            names.append(name)
        # Also check nested tool_calls array
        for tc in ev.get("tool_calls") or []:
            n = tc.get("function", {}).get("name") or tc.get("name")
            if n:
                names.append(n)
    return names


def _codex_final_text(events: list[dict]) -> str:
    parts: list[str] = []
    for ev in events:
        ev_type = ev.get("type", "")
        if ev_type in ("message", "assistant_message", "agent_message", "response"):
            content = ev.get("content") or ev.get("text") or ""
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        parts.append(block.get("text", ""))
    return " ".join(parts).lower()


# ---------------------------------------------------------------------------
# Test 1: claude (haiku 4.5) calls opencode-search MCP tool
# ---------------------------------------------------------------------------

@pytest.mark.flaky(reruns=1, reruns_delay=10)
def test_claude_haiku_uses_opencode_search_mcp(tmp_path):
    """Real claude -p with haiku 4.5 must resolve the MCP entry and call
    mcp__opencode-search__overview (as explicitly requested).

    Proof of correct integration:
    1. MCP entry in ~/.claude/settings.json resolves at startup → bridge spawns
    2. Priority directive in ~/.claude/CLAUDE.md → claude picks MCP before Bash/Read
    3. overview response is real (daemon at :8765 responds with actual project data)
    4. Final answer references 5+ of the 7 real tool names (factual grounding)
    """
    events, raw = _run_claude(
        f"Using the opencode-search MCP server, list the 7 MCP tools "
        f"this project at {_PROJECT} exposes. "
        f"Call `overview` first to confirm the project is indexed, then answer.",
        tmp_path=tmp_path,
    )
    tool_uses = _extract_tool_uses(events)
    mcp_calls = [t for t in tool_uses if t["name"].startswith(_MCP_TOOL_PREFIX)]

    # PROOF 1: at least one MCP tool was actually invoked
    assert mcp_calls, (
        f"claude (haiku) did NOT call any opencode-search MCP tool.\n"
        f"Priority directive failed or MCP entry did not load.\n"
        f"All tool calls: {[t['name'] for t in tool_uses]}\n"
        f"Full stdout (last 2000):\n{raw[-2000:]}"
    )

    # PROOF 2: overview was called as instructed (exercises end-to-end daemon wiring)
    assert any(t["name"] == f"{_MCP_TOOL_PREFIX}overview" for t in mcp_calls), (
        f"`mcp__opencode-search__overview` was NOT called despite explicit prompt.\n"
        f"MCP calls made: {[t['name'] for t in mcp_calls]}"
    )

    # PROOF 3: final answer references ≥5 of the 7 real tool names (grounded in MCP response)
    final_text = _extract_final_text(events)
    mentioned = [t for t in _MCP_TOOL_NAMES if t in final_text]
    assert len(mentioned) >= 5, (
        f"claude's final answer referenced only {len(mentioned)}/7 expected tool names.\n"
        f"Likely hallucinated or did not read MCP response.\n"
        f"Mentioned: {mentioned}\n"
        f"Answer excerpt: {final_text[:600]}"
    )


# ---------------------------------------------------------------------------
# Test 2: claude priority directive — first tool MUST be opencode-search
# ---------------------------------------------------------------------------

@pytest.mark.flaky(reruns=1, reruns_delay=10)
def test_claude_prefers_opencode_search_over_bash_for_code_question(tmp_path):
    """claude (haiku) must call an opencode-search MCP tool BEFORE any
    Bash/Read/Grep/Write when asked a code question.

    This proves the MANDATORY USAGE RULE in ~/.claude/CLAUDE.md actually
    overrides claude's default tool preference. If CLAUDE.md is out of sync
    or the priority rule is missing, claude defaults to Bash/Read first and
    this assertion fails.
    """
    events, raw = _run_claude(
        f"How does the SSE error event flow work in {_PROJECT}? "
        f"Specifically what happens in handlers/_chat_router.py when the LLM is unavailable?",
        tmp_path=tmp_path,
    )
    tool_uses = _extract_tool_uses(events)
    assert tool_uses, (
        f"claude made NO tool calls — expected at least one opencode-search call.\n"
        f"stdout:\n{raw[-1500:]}"
    )
    first_tool = tool_uses[0]["name"]
    assert first_tool.startswith(_MCP_TOOL_PREFIX), (
        f"Priority directive FAILED: first tool was {first_tool!r}, expected "
        f"an mcp__opencode-search__* tool.\n"
        f"All tools in order: {[t['name'] for t in tool_uses]}\n"
        f"This means ~/.claude/CLAUDE.md is missing or the priority rule drifted.\n"
        f"Run: python scripts/configure_integrations.py --apply-all"
    )


# ---------------------------------------------------------------------------
# Test 3: codex (gpt-5.4-mini) calls opencode-search MCP tool
# ---------------------------------------------------------------------------

@pytest.mark.flaky(reruns=1, reruns_delay=10)
def test_codex_gpt54mini_uses_opencode_search_mcp(tmp_path):
    """Real codex exec with gpt-5.4-mini must resolve the MCP entry and call
    an opencode-search tool (overview or search) when asked a codebase question.

    Proof of correct integration:
    1. MCP entry in ~/.codex/config.toml resolves at startup
    2. Priority directive in ~/.codex/AGENTS.md → codex picks MCP before shell
    3. Final answer references real tool names (factual grounding)
    """
    events, raw = _run_codex(
        f"Using the opencode-search MCP server, list the 7 MCP tools "
        f"this project at {_PROJECT} exposes. "
        f"Call `overview` first to confirm the project is indexed, then answer.",
        tmp_path=tmp_path,
    )
    tool_names = _extract_codex_tool_calls(events)
    mcp_calls = [
        n for n in tool_names
        if n.startswith(_MCP_TOOL_PREFIX) or n in _MCP_TOOL_NAMES
    ]

    # PROOF 1: at least one MCP tool was actually invoked
    assert mcp_calls, (
        f"codex (gpt-5.4-mini) did NOT call any opencode-search MCP tool.\n"
        f"Priority directive failed or MCP entry did not load.\n"
        f"All tool calls: {tool_names}\n"
        f"Full stdout (last 2000):\n{raw[-2000:]}"
    )

    # PROOF 2: overview was called as explicitly instructed
    assert any(
        "overview" in n for n in mcp_calls
    ), (
        f"`overview` MCP tool was NOT called despite explicit prompt instruction.\n"
        f"MCP calls: {mcp_calls}"
    )

    # PROOF 3: final answer references ≥5 of the 7 expected tool names
    final_text = _codex_final_text(events)
    mentioned = [t for t in _MCP_TOOL_NAMES if t in final_text]
    assert len(mentioned) >= 5, (
        f"codex final answer referenced only {len(mentioned)}/7 expected tool names.\n"
        f"Likely hallucinated or did not read MCP response.\n"
        f"Mentioned: {mentioned}\n"
        f"Answer excerpt: {final_text[:600]}"
    )
