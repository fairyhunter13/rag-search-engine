"""Canonical configuration source of truth for opencode-search integrations.

Every config tree (claude, claude-account1, claude-account2, codex, hermes,
opencode-default, opencode-personal) is derived from the constants here.
configure_integrations.py uses this module to verify and repair drift.
"""
from __future__ import annotations

from pathlib import Path

_VENV_PYTHON = str(
    Path(__file__).parent.parent.parent / ".venv" / "bin" / "python"
)

# ---------------------------------------------------------------------------
# Canonical MCP entry
# ---------------------------------------------------------------------------

CANONICAL_MCP_COMMAND = _VENV_PYTHON
CANONICAL_MCP_ARGS = ["-m", "opencode_search", "daemon", "bridge-stdio"]
CANONICAL_MCP_ENV = {
    "OPENCODE_ALLOW_INDEX_OUTSIDE_CWD": "1",
    "OPENCODE_LLM_PROVIDER": "ollama",
    "OPENCODE_LLM_MODEL": "qwen3-enrich:1.7b",
    "OPENCODE_QUERY_LLM_PROVIDER": "ollama",
    "OPENCODE_QUERY_LLM_MODEL": "qwen3-query:8b",
}

# ---------------------------------------------------------------------------
# Global lean-gate enforcement (hook path + deny globs for ~/.claude/settings.json)
# ---------------------------------------------------------------------------

from pathlib import Path as _Path

LEAN_GATE_HOOK_PATH: str = str(_Path.home() / ".claude" / "hooks" / "lean-gate.sh")

# ---------------------------------------------------------------------------
# Sentinels per file type
# ---------------------------------------------------------------------------

SENTINEL_CLAUDE_START = "<!-- >>> opencode-search global instructions >>> -->"
SENTINEL_CLAUDE_END   = "<!-- <<< opencode-search global instructions <<< -->"
SENTINEL_AGENTS_START      = "[opencode-search-global-instructions:start]"
SENTINEL_AGENTS_END        = "[opencode-search-global-instructions:end]"
SENTINEL_LEAN_START        = "<!-- >>> lean-change mandate >>> -->"
SENTINEL_LEAN_END          = "<!-- <<< lean-change mandate <<< -->"
SENTINEL_LEAN_AGENTS_START = "[lean-change-mandate:start]"
SENTINEL_LEAN_AGENTS_END   = "[lean-change-mandate:end]"

# ---------------------------------------------------------------------------
# Canonical lean-change mandate (Claude profiles only — names the Claude skill)
# ---------------------------------------------------------------------------

LEAN_BODY = (
    "MANDATORY: always enforce and implement the lean-change skill by default"
    " — make every change the smallest, simplest, most surgical diff that works;"
    " each line is a liability."
)

# ---------------------------------------------------------------------------
# Canonical system prompt body (shared across all tool types)
# ---------------------------------------------------------------------------

CANONICAL_BODY = """\
MANDATORY: Use the opencode-search MCP server as the primary code lookup tool whenever the current project is indexed.

5-tool API (v3 — June 2026 Phase 100): search · ask · graph · overview · index
See MCP tool schemas for full parameter reference (scope/relation/what variants, etc.).

Rules (no exceptions):
- Call search/ask/graph/overview BEFORE any Bash grep/find, Glob, or Grep tool call.
- Never delegate codebase questions to sub-agents via the Agent tool.
- GPU-only inference — CPU fallback is forbidden for all OSE operations.
- RESILIENCE: if an MCP call returns {"status":"timeout","fallback":true} or hangs/errors,
  immediately fall back to native Read/Grep/Glob/Bash — never wait or retry the MCP call.
- NEVER auto-index. Only call index(enabled=True) when the user explicitly asks.
- If not indexed, say so and ask before indexing.\
"""

# Full block including sentinels, for each file type.

def claude_block() -> str:
    """Return the canonical sentinel-wrapped block for CLAUDE.md files."""
    return f"{SENTINEL_CLAUDE_START}\n{CANONICAL_BODY}\n{SENTINEL_CLAUDE_END}\n"


def agents_md_block() -> str:
    """Return the canonical sentinel-wrapped block for AGENTS.md files."""
    return f"{SENTINEL_AGENTS_START}\n{CANONICAL_BODY}\n{SENTINEL_AGENTS_END}\n"


def lean_claude_block() -> str:
    """Return the canonical sentinel-wrapped lean-change mandate for CLAUDE.md files."""
    return f"{SENTINEL_LEAN_START}\n{LEAN_BODY}\n{SENTINEL_LEAN_END}\n"


def lean_agents_block() -> str:
    """Return the canonical sentinel-wrapped lean-change mandate for AGENTS.md / hermes."""
    return f"{SENTINEL_LEAN_AGENTS_START}\n{LEAN_BODY}\n{SENTINEL_LEAN_AGENTS_END}\n"


# ---------------------------------------------------------------------------
# Canonical lean-change SKILL.md (cross-agent: Claude, codex, opencode, hermes)
# ---------------------------------------------------------------------------

LEAN_SKILL_MD = """\
---
name: lean-change
description: Enforce the smallest possible surgical diff — less is more, each line is a liability. Load on demand for the 6-check checklist.
---

# Lean-Change

**Creed:** the best change is none. The next best is a deletion. Then the fewest lines that work.

## 6 checks before every edit (all required)

1. **Search first** — call `opencode-search` before adding anything. Reuse existing code.
2. **Need it?** — satisfy the requirement by reusing, configuring, or deleting before adding code.
3. **Smallest diff** — minimum change. No speculative abstraction. No unrequested refactor. No "while I'm here." Plainest construct (KISS).
4. **No new dependency** without a one-line justification tied to the requirement. Prefer stdlib or already-vendored.
5. **Reduction pass (MANDATORY)** — after drafting, re-read and cut every non-load-bearing line. Aim net-negative. Delete dead code; never comment it out.
6. **Verify** — `go vet ./... && go test ./...` or `npx tsc --noEmit` green before commit.

The `PreToolUse` hook auto-enforces the diff budget (≤40 net lines on existing files, ≤150 on new).
"""
