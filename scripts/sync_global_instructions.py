"""Sync the canonical opencode-search global instructions to all 4 MCP clients.

Idempotent: replaces content between [start]/[end] markers on every run.
Clients updated:
  - Claude Code  (~/.claude/CLAUDE.md, ~/CLAUDE.md, project CLAUDE.md)
  - Codex        (~/.config/codex/config.toml  [developer_instructions])
  - opencode     (~/.config/opencode/opencode.jsonc [prompt field])
  - hermes       (~/.hermes/config.yaml         [agent.system_prompt])
"""
from __future__ import annotations
import json
import os
import re
import sys
import tomllib
from pathlib import Path

# ─── Canonical instruction text ───────────────────────────────────────────────
# This is the single source of truth. Update here; run the script to propagate.

CANONICAL = """\
MANDATORY: Use the opencode-search MCP server as the primary code lookup tool whenever the current project is indexed.

7-tool intent API (v2) — pick the right tool:
- `search(query, scope, project_paths)` — find SPECIFIC code/files/functions. scope: "code" (default)|"docs"|"all"
- `ask(query, project_path, scope)` — 'how does X work?', architecture, business process. scope: "all" (default)|"architecture"|"wiki"
- `graph(symbol, project_path, relation)` — callers, callees, impact, call path. relation: "definition"|"callers"|"callees"|"impact"|"path"
- `overview(project_path, what)` — structure, communities, status, project list, metrics. what: "structure"|"communities"|"status"|"projects"|"metrics"
- `build(project_path, action)` — index, pipeline (full KB build), enrich, wiki, ingest docs. action: "pipeline" (default, recommended first-run)
- `federation(root_path, action)` — discover/list/add/remove/index federation sub-repos
- `manage(project_path, action)` — stop_watching, wiki_lint

Rules (no exceptions):
- Before running ANY Bash command that searches code or text — grep, rg, ag, find -name/-exec, glob, fd, or similar — FIRST call `search` with a natural language query. Only fall back to bash search commands if `search` returns no useful results or the project is not indexed.
- Before reading, editing, or answering questions about ANY file or codebase topic: call `search` first. Do NOT go straight to Bash/grep/find/Read for codebase exploration.
- When answering a user question, prefer using the user's question text verbatim as the initial `search` query. For architectural questions, use `ask` instead.
- In your final answer, reference specific file paths and identifiers found in search results so the answer is grounded and unambiguous.
- Do NOT delegate codebase questions to sub-agents via the Agent tool — sub-agents do not inherit these instructions. Call the tools yourself, directly.
- Never auto-index a project. Only call `build(action="index")` or `build(action="pipeline")` when the user explicitly asks to index/setup the project.
- After a project has been explicitly indexed, rely on the daemon's automatic watch behavior.\
"""

START_MARKER = "[opencode-search-global-instructions:start]"
END_MARKER = "[opencode-search-global-instructions:end]"
MD_START = "<!-- >>> opencode-search global instructions >>> -->"
MD_END = "<!-- <<< opencode-search global instructions <<< -->"


def _wrap_plain(text: str) -> str:
    return f"{START_MARKER}\n{text}\n{END_MARKER}"


def _wrap_md(text: str) -> str:
    return f"{MD_START}\n{text}\n{MD_END}\n"


def _replace_between(content: str, start: str, end: str, replacement: str) -> str:
    pattern = re.compile(
        re.escape(start) + r".*?" + re.escape(end),
        re.DOTALL,
    )
    wrapped = f"{start}\n{replacement}\n{end}"
    if pattern.search(content):
        return pattern.sub(wrapped, content)
    return content + "\n" + wrapped + "\n"


# ─── Update ~/.claude/CLAUDE.md (and ~/CLAUDE.md, project CLAUDE.md) ──────────

def update_md_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        content = path.read_text()
        new = _replace_between(content, MD_START, MD_END, text)
    else:
        new = _wrap_md(text)
    path.write_text(new)
    print(f"  ✓ {path}")


# ─── Update ~/.config/codex/config.toml [developer_instructions] ─────────────

def update_codex_toml(path: Path, text: str) -> None:
    if not path.exists():
        print(f"  ! {path} not found — skipping")
        return
    content = path.read_text()
    wrapped = f"{START_MARKER}\n{text}\n{END_MARKER}"
    pattern = re.compile(
        r'developer_instructions\s*=\s*"[^"]*(' + re.escape(START_MARKER) + r').*?(' + re.escape(END_MARKER) + r')[^"]*"',
        re.DOTALL,
    )
    escaped = wrapped.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    new_value = f'developer_instructions = "{escaped}"'

    # Simpler: replace just the instructions block inside the string value
    # Find developer_instructions line and rebuild it
    lines = content.splitlines(keepends=True)
    out = []
    i = 0
    replaced = False
    while i < len(lines):
        line = lines[i]
        if line.startswith("developer_instructions") and not replaced:
            # Collect multiline value
            val_lines = [line]
            while not val_lines[-1].endswith('"\n') and not val_lines[-1].endswith('"'):
                i += 1
                val_lines.append(lines[i])
            replaced = True
            out.append(new_value + "\n")
        else:
            out.append(line)
        i += 1
    if not replaced:
        out.append(new_value + "\n")
    path.write_text("".join(out))
    print(f"  ✓ {path}")


# ─── Update ~/.config/opencode/opencode.jsonc ─────────────────────────────────

def update_opencode_jsonc(path: Path, text: str) -> None:
    if not path.exists():
        print(f"  ! {path} not found — skipping")
        return
    content = path.read_text()
    # opencode.jsonc has no instruction field to update — it registers the MCP server.
    # The instructions come from the daemon's FastMCP `instructions=` param and from CLAUDE.md.
    # No change needed; just report.
    print(f"  ✓ {path} (MCP server registration — instructions come from daemon + CLAUDE.md)")


# ─── Update ~/.hermes/config.yaml [agent.system_prompt] ──────────────────────

def update_hermes_yaml(path: Path, text: str) -> None:
    if not path.exists():
        print(f"  ! {path} not found — skipping")
        return
    content = path.read_text()
    wrapped = f"{START_MARKER}\n{text}\n{END_MARKER}"
    pattern = re.compile(
        re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
        re.DOTALL,
    )
    if pattern.search(content):
        new = pattern.sub(wrapped, content)
    else:
        # Insert after "system_prompt: " line
        new = re.sub(
            r'(system_prompt:\s*")([^"]*)"',
            lambda m: m.group(1) + wrapped.replace('"', '\\"').replace("\n", "\\n") + '"',
            content,
        )
    path.write_text(new)
    print(f"  ✓ {path}")


# ─── Also update the FastMCP instructions= in daemon.py ──────────────────────

def update_daemon_prompt(repo: Path, text: str) -> None:
    daemon_path = repo / "src/opencode_search/daemon.py"
    if not daemon_path.exists():
        print(f"  ! {daemon_path} not found")
        return
    content = daemon_path.read_text()
    wrapped = f"{START_MARKER}\n{text}\n{END_MARKER}"
    pattern = re.compile(
        re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
        re.DOTALL,
    )
    if pattern.search(content):
        new = pattern.sub(wrapped, content)
        daemon_path.write_text(new)
        print(f"  ✓ {daemon_path}")
    else:
        print(f"  ! {daemon_path}: markers not found — skipping (run manually)")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    home = Path.home()
    repo = Path(__file__).parent.parent

    text = CANONICAL

    print("Syncing opencode-search global instructions to all clients...")

    # Claude Code global + project CLAUDE.md files
    for md in [
        home / ".claude" / "CLAUDE.md",
        home / "CLAUDE.md",
        repo / "CLAUDE.md",
    ]:
        update_md_file(md, text)

    # Codex config.toml
    update_codex_toml(home / ".config" / "codex" / "config.toml", text)

    # opencode jsonc (no instruction field, just note)
    update_opencode_jsonc(home / ".config" / "opencode" / "opencode.jsonc", text)

    # hermes config.yaml
    update_hermes_yaml(home / ".hermes" / "config.yaml", text)

    # Daemon prompt (embedded in daemon.py _global_prompt_text)
    update_daemon_prompt(repo, text)

    print("\nDone. Restart the daemon to pick up prompt changes:")
    print("  systemctl --user restart opencode-search-mcp-daemon.service")


if __name__ == "__main__":
    main()
