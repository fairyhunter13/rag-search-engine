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
- After indexing, the daemon watches files automatically.

RESILIENCE — IMPORTANT:
- opencode-search calls have a built-in ~5s deadline. If a call returns {"status":"timeout","fallback":true}, or hangs/errors, IMMEDIATELY fall back to native Read/Grep/Glob/Bash tools. Never wait or retry the MCP call.
- Do NOT let a slow or stuck opencode-search call block answering the user. Native tools are always the fallback.\
"""

# Full block including sentinels, for each file type.

def claude_block() -> str:
    """Return the canonical sentinel-wrapped block for CLAUDE.md files."""
    return f"{SENTINEL_CLAUDE_START}\n{CANONICAL_BODY}\n{SENTINEL_CLAUDE_END}\n"


def agents_md_block() -> str:
    """Return the canonical sentinel-wrapped block for AGENTS.md files."""
    return f"{SENTINEL_AGENTS_START}\n{CANONICAL_BODY}\n{SENTINEL_AGENTS_END}\n"
