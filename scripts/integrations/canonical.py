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
# Sentinels per file type
# ---------------------------------------------------------------------------

SENTINEL_CLAUDE_START = "<!-- >>> opencode-search global instructions >>> -->"
SENTINEL_CLAUDE_END   = "<!-- <<< opencode-search global instructions <<< -->"
SENTINEL_AGENTS_START = "[opencode-search-global-instructions:start]"
SENTINEL_AGENTS_END   = "[opencode-search-global-instructions:end]"

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
