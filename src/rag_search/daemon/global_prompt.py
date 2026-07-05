"""MCP tool instructions for the rag-search server.

_PROMPT mirrors CANONICAL_BODY from scripts/integrations/canonical.py — update
both files in sync when doctrine changes. Single write path: configure_integrations.py
writes to ~/.claude{,-account1,-account2}/CLAUDE.md; bare ~/CLAUDE.md is NOT written.
"""
from __future__ import annotations

from pathlib import Path

_START = "[rag-search-global-instructions:start]"
_END = "[rag-search-global-instructions:end]"

# Canonical MCP doctrine (June 2026 Phase 100). Mirrors scripts/integrations/canonical.py.
_PROMPT = """\
MANDATORY: Use the rag-search MCP server as the primary code lookup tool whenever the current project is indexed.

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


def _inject(text: str, prompt: str) -> str:
    block = f"{_START}\n{prompt}\n{_END}"
    if _START in text:
        s = text.index(_START)
        e = text.index(_END) + len(_END)
        return text[:s] + block + text[e:]
    return text.rstrip("\n") + "\n\n" + block + "\n"


def inject_claude_md(path: Path | None = None) -> None:
    p = path or Path.home() / "CLAUDE.md"
    existing = p.read_text() if p.exists() else ""
    p.write_text(_inject(existing, _PROMPT))


def remove_claude_md(path: Path | None = None) -> None:
    p = path or Path.home() / "CLAUDE.md"
    if not p.exists():
        return
    text = p.read_text()
    if _START not in text:
        return
    s = text.index(_START)
    end = text.index(_END) + len(_END)
    p.write_text(text[:s].rstrip("\n") + "\n" + text[end:])
