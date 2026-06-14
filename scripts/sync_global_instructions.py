"""Sync the canonical opencode-search global instructions to all 4 MCP clients.

Idempotent: replaces content between [start]/[end] markers on every run.
Clients updated:
  - Claude Code  (~/.claude/CLAUDE.md, ~/CLAUDE.md, project CLAUDE.md)
  - Codex        (~/.config/codex/config.toml  [developer_instructions])
  - opencode     (~/.config/opencode/opencode.jsonc [prompt field])
  - hermes       (~/.hermes/config.yaml         [agent.system_prompt])
"""
from __future__ import annotations
import re
from pathlib import Path

# ─── Canonical instruction text ───────────────────────────────────────────────
# This is the single source of truth. Update here; run the script to propagate.

CANONICAL = """\
MANDATORY: Use the opencode-search MCP server as the primary code lookup tool whenever the current project is indexed.

5-tool intent API (v3 — June 2026 Phase 100):
- `search(query, scope, project_paths)` — find SPECIFIC code/files/functions. scope: "code" (default)|"docs"|"all"
- `ask(query, project_path, scope)` — 'how does X work?', architecture, design. scope: "all" (default)|"architecture"|"wiki"|"global"|"feature"
  - scope="global": GraphRAG map-reduce synthesis across ALL community summaries
  - scope="feature": entry points + call chain + algorithm overview + design rationale (WHY it was built this way)
- `graph(symbol, project_path, relation)` — call graph analysis
  - relation: "callers"|"callees"|"impact"|"path" — standard
  - relation: "impact_narrative" — LLM summary of blast radius: risk level, affected domains
  - relation: "semantic_trace" (+to_symbol=) — natural language trace between two symbols
- `overview(project_path, what)` — project overview
  - what: "structure"|"communities"|"status"|"projects"|"patterns" — standard
  - what: "architecture_domains" — top-level Leiden hierarchy
  - what: "hierarchy" — full recursive Leiden hierarchy (all levels)
  - what: "service_mesh" — detected inter-service gRPC/HTTP/MQ topology
  - what: "import_cycles" — circular import dependencies
  - what: "suggested_questions" — questions the graph is uniquely positioned to answer
  - what: "graph_diff" — symbols added/removed recently
  - what: "surprising_connections" — edges spanning architectural community boundaries
- `index(project_path, enabled)` — THE ONLY WRITE TOOL: flag project for indexing
  - enabled=True → register project; daemon auto-indexes, builds KB, watches, indexes federation members
  - enabled=False → DESTRUCTIVE: stop watching + remove from registry + delete all on-disk index data

QUICK DECISION GUIDE:
  'find the payment handler'           → search('payment handler')
  'how does auth work?'                → ask('how does auth work', project_path)
  'what is the overall architecture?'  → ask('describe architecture', project_path, scope='global')
  'what calls ProcessOrder?'           → graph('ProcessOrder', project_path, relation='callers')
  'what breaks if I change X?'         → graph('X', project_path, relation='impact_narrative')
  'trace login to database'            → graph('login', project_path, relation='semantic_trace', to_symbol='database write')
  'what services call each other?'     → overview(project_path, what='service_mesh')
  'top-level architecture domains?'    → overview(project_path, what='architecture_domains')
  'are there circular imports?'        → overview(project_path, what='import_cycles')
  'what changed in the graph?'         → overview(project_path, what='graph_diff')
  'unusual cross-layer dependencies?'  → overview(project_path, what='surprising_connections')
  'what should I explore first?'       → overview(project_path, what='suggested_questions')
  'tell me about this project'         → overview(project_path, what='structure')
  'what packages/dependencies?'        → overview(project_path, what='patterns')
  'list all indexed projects'          → overview(what='projects')
  'index this project' [explicit ask]  → index(project_path, enabled=True)
  'how does checkout feature work?'    → ask('how does checkout work', project_path, scope='feature')
  'why is auth designed this way?'     → ask('why auth uses JWT', project_path, scope='feature')

Rules (no exceptions):
- Before running ANY Bash command that searches code or text — FIRST call `search` with a natural language query.
- Before reading, editing, or answering questions about ANY file or codebase topic: call `search` first.
- Use ask(scope="global") for holistic questions about the entire codebase.
- Use graph(relation="impact_narrative") for human-readable blast radius analysis.
- In your final answer, reference specific file paths and identifiers found in search results.
- Do NOT delegate codebase questions to sub-agents via the Agent tool.
- NEVER auto-index. Only call `index(enabled=True)` when the user explicitly asks.
- If not indexed, say so and ask before indexing.
- After indexing, the daemon watches files automatically.\
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
    re.compile(
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
    path.read_text()
    # opencode.jsonc has no instruction field to update — it registers the MCP server.
    # The instructions come from the daemon's FastMCP `instructions=` param and from CLAUDE.md.
    # No change needed; just report.
    print(f"  ✓ {path} (MCP server registration — instructions come from daemon + CLAUDE.md)")


# ─── Update ~/.hermes/config.yaml [agent.system_prompt] ──────────────────────

def update_hermes_yaml(path: Path, text: str) -> None:
    """Update hermes config system_prompt with proper YAML block scalar indentation.

    YAML block scalars require content indented past the key level (4 spaces here).
    Backticks and special chars are safe inside a literal block scalar (`|`) but
    we normalise them to avoid any YAML parser quirks.
    """
    if not path.exists():
        print(f"  ! {path} not found — skipping")
        return
    content = path.read_text()

    # Build a YAML-safe version: replace backticks with single-quotes and
    # use ASCII arrows so no special-char scanner issues in any YAML version.
    safe_text = (text
                 .replace("`", "'")
                 .replace("→", "->")
                 .replace("—", "--"))

    wrapped_lines = f"{START_MARKER}\n{safe_text}\n{END_MARKER}"
    # Indent every line by 4 spaces for YAML block scalar under `system_prompt: |`
    indented = "\n".join("    " + ln for ln in wrapped_lines.splitlines())

    # Pattern matches the full system_prompt block scalar (| or |-) plus its content
    block_pattern = re.compile(
        r"(  system_prompt:\s*\|[-]?\n)    " + re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER) + r"\n?",
        re.DOTALL,
    )
    if block_pattern.search(content):
        new = block_pattern.sub(r"\1" + indented + "\n", content)
    else:
        # Replace whatever system_prompt block exists with our managed block
        any_block = re.compile(r"(  system_prompt:.*?\n)(?=\S|\Z)", re.DOTALL)
        replacement = f"  system_prompt: |\n{indented}\n"
        if any_block.search(content):
            new = any_block.sub(replacement, content)
        else:
            new = content.rstrip() + f"\nagent:\n  system_prompt: |\n{indented}\n"
    path.write_text(new)
    print(f"  ✓ {path}")


# ─── Update ~/.codex/AGENTS.md ────────────────────────────────────────────────

def update_codex_agents_md(path: Path, text: str) -> None:
    """Rewrite ~/.codex/AGENTS.md with the full v2 API in markdown format."""
    if not path.parent.exists():
        print(f"  ! {path.parent} not found — skipping AGENTS.md update")
        return
    content = (
        "# Code Intelligence Tools (opencode-search)\n\n"
        "You have access to the **opencode-search** MCP server for deep code intelligence.\n"
        "Use these tools for ALL code exploration tasks — they are faster and more accurate "
        "than grep or file reading.\n\n"
        f"{START_MARKER}\n{text}\n{END_MARKER}\n"
    )
    path.write_text(content)
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

    # Codex config.toml — codex stores config at ~/.codex/, NOT ~/.config/codex/
    update_codex_toml(home / ".codex" / "config.toml", text)

    # Codex AGENTS.md — global markdown instructions read by codex on every session
    update_codex_agents_md(home / ".codex" / "AGENTS.md", text)

    # opencode jsonc (no instruction field, just note)
    update_opencode_jsonc(home / ".config" / "opencode" / "opencode.jsonc", text)

    # hermes config.yaml
    update_hermes_yaml(home / ".hermes" / "config.yaml", text)

    # daemon.py _global_prompt_text() is maintained manually — do NOT auto-sync,
    # as regex replacement would corrupt the Python string constant definitions.

    print("\nDone. Restart the daemon to pick up prompt changes:")
    print("  systemctl --user restart opencode-search-mcp-daemon.service")


if __name__ == "__main__":
    main()
