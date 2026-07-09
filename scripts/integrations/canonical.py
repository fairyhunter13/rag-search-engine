"""Canonical configuration source of truth for rag-search integrations.

The constants here are written into all detected config trees (claude profiles,
hermes). configure_integrations.py uses this module to verify and repair drift.
OpenCode and Codex integration removed.
"""
from __future__ import annotations

from pathlib import Path as _Path

# ---------------------------------------------------------------------------
# Canonical MCP entry (HTTP transport — daemon at :8765)
# ---------------------------------------------------------------------------

CANONICAL_MCP_URL = "http://127.0.0.1:8765/mcp"

# ---------------------------------------------------------------------------
# Sentinels per file type
# ---------------------------------------------------------------------------

SENTINEL_CLAUDE_START = "<!-- >>> rag-search global instructions >>> -->"
SENTINEL_CLAUDE_END   = "<!-- <<< rag-search global instructions <<< -->"
SENTINEL_AGENTS_START      = "[rag-search-global-instructions:start]"
SENTINEL_AGENTS_END        = "[rag-search-global-instructions:end]"

# ---------------------------------------------------------------------------
# Canonical system prompt body (shared across all tool types)
# ---------------------------------------------------------------------------

CANONICAL_BODY = """\
MANDATORY: Use the rag-search MCP server as the primary code lookup tool whenever the current project is indexed.

5-tool API (v3 — June 2026 Phase 100): search · ask · graph · overview · index
See MCP tool schemas for full parameter reference (scope/relation/what variants, etc.).

Rules (no exceptions):
- Call search/ask/graph/overview BEFORE any Bash grep/find, Glob, or Grep tool call.
- Never delegate codebase questions to sub-agents via the Agent tool.
- GPU-only inference — CPU fallback is forbidden for all RSE operations.
- RESILIENCE: if an MCP call returns {"status":"timeout","fallback":true} or hangs/errors,
  immediately fall back to native Read/Grep/Glob/Bash — never wait or retry the MCP call.
- NEVER auto-index. Only call index(enabled=True) when the user explicitly asks.
- If not indexed, say so and ask before indexing.\
"""

