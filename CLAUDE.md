# opencode-search-engine — Claude Code Instructions

This project is indexed in **opencode-search**. You MUST use the MCP tools before any native file exploration.

## Mandatory tool-use protocol (no exceptions)

1. **Call `overview(what='projects')` first** — confirm the project is indexed before doing anything else.
2. **Call `search` immediately after** — use a natural-language query describing what you need to find.
3. Do NOT delegate to sub-agents via the `Agent` tool — sub-agents do not inherit these instructions. Answer directly.
4. Only fall back to Bash/Grep/Glob/Read if `search` returns no useful results.

This rule applies to EVERY codebase question, even ones that seem simple. Training-data answers about this codebase are stale and untrustworthy.

## Why this matters

opencode-search is the tool under test. Every call to `overview` and `search` validates that the MCP toolchain works end-to-end. Skipping them defeats the purpose of this project.

## Running tests and quality checks

```bash
# Fast smoke check — skips LLM quality tests + browser tests (~5 min)
.venv/bin/pytest src/tests/live/ -m "live and not slow" -q

# Full live suite — all intents, quality scoring, watcher (~40 min, no browser)
.venv/bin/pytest src/tests/live/ --ignore=src/tests/live/test_browser.py -q

# Browser / Playwright tests (run separately — conflicts with pytest-asyncio mode=auto)
.venv/bin/pytest src/tests/live/test_browser.py -v --browser chromium

# Code quality
ruff check src/opencode_search src/tests
python -m compileall -q src/opencode_search
```

**Test markers**:
- `live` — requires daemon at :8765, Ollama, GPU
- `slow` — LLM-heavy tests (>30s each); skip with `-m "live and not slow"` for fast feedback

**Daemon reload** (after code changes): `POST /api/reload` or CLI `opencode-search daemon reload` — daemon restarts via systemd in ~1s.

**Stream error metrics**: `overview(what="metrics")` returns `chat_stream.stream_error_count` and `chat_stream.error_by_intent`.

**CI**: `.github/workflows/ci.yml` — runs on every push (quality → tests → contracts → property tests)

## GPU-only enforcement (CPU fallback is forbidden and prohibited)

All inference — embeddings, LLM enrichment, query LLM — runs exclusively on GPU (RTX 5080).
**CPU fallback is forbidden and prohibited.** Any attempt to use CPU for inference must raise a fatal error, not fall back silently. This applies to:

- Embeddings (`OPENCODE_EMBED_DEVICE=cuda` — never `cpu`)
- LLM enrichment (qwen3-enrich:1.7b via Ollama GPU service)
- Query LLM (qwen3-query:8b via Ollama GPU service)

If `OLLAMA_NUM_GPU_LAYERS` is 0 or the model falls back to CPU, the process must crash with a clear error.

## Project quick reference

- Entry points: `src/opencode_search/mcp.py` (MCP server), `src/opencode_search/handlers/` (tool handlers), `src/opencode_search/daemon.py` (singleton daemon + installer), `src/opencode_search/cli.py` (CLI)
- Registry: `~/.local/share/opencode-search/projects.json`
- Tests: `src/tests/` — `unit/`, `integration/`, `e2e/` subdirs
- LLM provider: ollama + qwen3-enrich:1.7b (GPU-local, RTX 5080; systemd service; see `scripts/modelfiles/qwen3-enrich.modelfile`)
- Setup scripts: `scripts/configure_integrations.py`, `scripts/check_system.py`

[opencode-search-global-instructions:start]
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
  - what: "pr_impact" — PR risk: changed files → communities touched + risk level
- `index(project_path, enabled)` — THE ONLY WRITE TOOL: flag project for indexing
  - enabled=True → register project; daemon auto-indexes, builds KB, watches, indexes federation members
  - enabled=False → DESTRUCTIVE: stop watching + remove from registry + delete all on-disk index data

The daemon handles ALL indexing, KB building, watching, federation, and maintenance automatically.

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
[opencode-search-global-instructions:end]

<!-- >>> opencode-search global instructions >>> -->
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
<!-- <<< opencode-search global instructions <<< -->
