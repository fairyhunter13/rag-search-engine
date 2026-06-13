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
MANDATORY: opencode-search is the PRIMARY code lookup tool on this machine.
Before invoking Read, Grep, Glob, or Bash for ANY code search — call search,
ask, graph, or overview FIRST. Only fall back to Read/Grep/Bash if the MCP
call returns no useful results. This rule has NO exceptions.

To onboard a brand-new repo in one step: `ocs-index <path>`
(builds the full KB: index → entity enrichment → community hierarchy → wiki)

opencode-search: GPU-accelerated code intelligence. 5 tools — pick the right one:

WHICH TOOL TO CALL:
  search(query, scope, project_paths)   → find specific code, files, functions. scope: code|docs|all
  ask(query, project_path, scope)       → 'how does X work?', architecture, conventions, business logic
    scope="global": GraphRAG map-reduce synthesis across ALL community summaries
    scope="feature": feature trace — entry points, call chain, algorithm, design rationale (WHY)
    scope="business": answer from business-classified communities (features, processes, rules)
  graph(symbol, project_path, relation) → call graph analysis
    relation="callers|callees|impact|path" — standard
    relation="impact_narrative"          — LLM summary: risk level, affected domains
    relation="semantic_trace" (+to_symbol=) — natural language trace between two symbols
  overview(project_path, what)          → project structure, communities, dependencies, status
    what="structure|communities|status|projects|patterns" — standard
    what="architecture_domains"          — top-level Leiden hierarchy (architecture domains)
    what="hierarchy"                     — full recursive Leiden hierarchy (all levels)
    what="service_mesh"                  — detected inter-service gRPC/HTTP/MQ topology
    what="import_cycles"                 — circular import dependencies (Tarjan SCC)
    what="suggested_questions"           — questions the graph is uniquely positioned to answer
    what="graph_diff"                    — symbols added/removed recently
    what="surprising_connections"        — edges spanning architectural community boundaries
    what="feature_map"                  — business knowledge map: all communities by semantic type
    what="business_rules"               — communities classified as constraints/policies/validations
    what="process_flows"                — communities classified as workflows/business processes
  index(project_path, enabled)          → flag a project for the search engine (THE ONLY WRITE TOOL)
    enabled=True  → register project; daemon auto-indexes, builds KB, watches, indexes federation members
    enabled=False → DESTRUCTIVE: stop watching + remove from registry + delete all on-disk index data

The daemon handles ALL indexing, KB building, watching, federation, and maintenance
automatically. No manual triggers needed. Everything stays healthy on its own.

QUICK DECISION GUIDE:
  'find the payment handler'           → search('payment handler')
  'how does auth work?'                → ask('how does auth work', project_path)
  'what is the overall architecture?'  → ask('describe architecture', project_path, scope='global')
  'how does checkout work end-to-end?' → ask('checkout feature', project_path, scope='feature')
  'why is this code designed this way?' → ask('why is X designed this way', project_path, scope='feature')
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
  'what business features exist?'      → overview(project_path, what='feature_map')
  'what business rules govern X?'      → ask('rules for X', project_path, scope='business')
  'what are the checkout workflows?'   → overview(project_path, what='process_flows')
  'list all business constraints'      → overview(project_path, what='business_rules')

RULES:
- Call search BEFORE grep/find/Read for any code lookup. Only fall back to bash if search returns nothing.
- Use ask for 'how does X work' questions; use search to find specific code.
- Use ask(scope="global") for holistic questions about the entire codebase.
- Use graph(relation="impact_narrative") instead of raw impact for human-readable analysis.
- overview(what='structure') returns the project tree, language breakdown, graph stats, and top communities.
- overview(what='patterns') returns languages, dependencies, package versions, coding conventions, frameworks, architecture, and module structure.
- NEVER auto-index. Only call index(enabled=True) when the user explicitly asks to index a project.
- If the project is not indexed, say so and ask before indexing.
- Do NOT delegate codebase questions to sub-agents — they don't inherit these instructions.
- After indexing, the daemon watches files automatically — no need to re-index on every change.

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
