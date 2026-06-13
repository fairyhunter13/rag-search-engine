"""Inject MCP tool instructions into ~/CLAUDE.md and editor configs."""
from __future__ import annotations

from pathlib import Path

_START = "[opencode-search-global-instructions:start]"
_END = "[opencode-search-global-instructions:end]"

_PROMPT = """\
MANDATORY: Use the opencode-search MCP server as the primary code lookup tool.

5-tool API (v3):
- search(query, scope, project_paths): find specific code/files/functions
- ask(query, project_path, scope): how does X work? architecture, design
- graph(symbol, project_path, relation): call graph analysis
- overview(project_path, what): project overview, patterns, communities
- index(project_path, enabled): register or remove a project

Rules: call search before grep/find; use ask for 'how does X work'; GPU-only."""


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
