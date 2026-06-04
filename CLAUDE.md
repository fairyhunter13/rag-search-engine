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
# All fast tests (no GPU, no live services)
.venv/bin/pytest src/tests/ -m "not (gpu or runtime_deps or large or embedder or indexer or slow or playwright)" -q

# Full test suite (all non-GPU)
.venv/bin/pytest src/tests/ -m "not (gpu or embedder or indexer or playwright)" -q

# Playwright browser E2E tests (must run separately — conflicts with pytest-asyncio mode=auto)
.venv/bin/pytest src/tests/e2e/test_dashboard_playwright.py -v --browser chromium

# MCP contracts + invariants only
.venv/bin/pytest src/tests/integration/test_mcp.py src/tests/integration/test_invariants.py -v

# Code quality
ruff check src/opencode_search src/tests
python -m compileall -q src/opencode_search
```

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

7-tool intent API (v2 — June 2026):
- `search(query, scope, project_paths)` — find SPECIFIC code/files/functions. scope: "code" (default)|"docs"|"all"
- `ask(query, project_path, scope)` — 'how does X work?', architecture, design. scope: "all" (default)|"architecture"|"wiki"|"global"
  - scope="global": GraphRAG map-reduce synthesis across ALL community summaries
- `graph(symbol, project_path, relation)` — call graph analysis
  - relation: "callers"|"callees"|"impact"|"path" — standard
  - relation: "impact_narrative" — LLM summary of blast radius: risk level, affected domains
  - relation: "semantic_trace" (+to_symbol=) — natural language trace between two symbols
- `overview(project_path, what)` — project overview
  - what: "structure"|"communities"|"status"|"projects"|"patterns" — standard
  - what: "architecture_domains" — top-level Leiden hierarchy
  - what: "hierarchy" — full recursive Leiden hierarchy (all levels)
  - what: "service_mesh" — detected inter-service gRPC/HTTP/MQ topology
- `build(project_path, action)` — index, pipeline (full KB build), enrich, wiki, ingest docs
  - action: "pipeline" (recommended first-run) | "hierarchy" (GraphRAG-like community hierarchy)
- `federation(root_path, action)` — discover/list/add/remove/index federation sub-repos
- `manage(project_path, action)` — stop_watching, wiki_lint

Rules (no exceptions):
- Before running ANY Bash command that searches code or text — FIRST call `search` with a natural language query.
- Before reading, editing, or answering questions about ANY file or codebase topic: call `search` first.
- Use ask(scope="global") for holistic questions about the entire codebase.
- Use graph(relation="impact_narrative") for human-readable blast radius analysis.
- In your final answer, reference specific file paths and identifiers found in search results.
- Do NOT delegate codebase questions to sub-agents via the Agent tool.
- NEVER auto-index. Only call `build` when the user explicitly asks.
- If not indexed, say so and ask before indexing.
- After indexing, the daemon watches files automatically.
[opencode-search-global-instructions:end]

<!-- >>> opencode-search global instructions >>> -->
MANDATORY: Use the opencode-search MCP server as the primary code lookup tool whenever the current project is indexed.

7-tool intent API (v2 — June 2026):
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
  - what: "import_cycles" — circular import dependencies (Tarjan SCC on file-level graph)
  - what: "suggested_questions" — questions the graph is uniquely positioned to answer
  - what: "graph_diff" — symbols added/removed recently
  - what: "surprising_connections" — edges spanning architectural community boundaries
  - what: "pr_impact" — PR risk: changed files → communities touched + risk level
- `build(project_path, action)` — index, pipeline (full KB build), enrich, wiki, ingest docs
  - action: "pipeline" (recommended first-run) | "hierarchy" (GraphRAG-like community hierarchy) | "analyze_patterns" (LLM deep analysis)
  - action: "enrich_hierarchy" — re-run LLM enrichment for level-2+ communities (fixes unenriched hierarchies)
- `federation(root_path, action)` — discover/list/add/remove/index federation sub-repos
- `manage(project_path, action)` — project lifecycle operations
  - action: "wiki_lint" | "stop_watching"
  - action: "install_hooks" — install git post-commit hook for auto-reindex
  - action: "uninstall_hooks" — remove git post-commit hook
  - action: "dedup" — deduplicate graph nodes (add dry_run=True to preview)
  - action: "vacuum" — remove orphan index tier dirs; free disk space
  - action: "remove_project" — remove project from registry (delete_index=True also removes on-disk index)

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
  'index this project' [explicit ask]  → build(project_path, action='pipeline')
  'how does checkout feature work?'    → ask('how does checkout work', project_path, scope='feature')
  'why is auth designed this way?'     → ask('why auth uses JWT', project_path, scope='feature')

Rules (no exceptions):
- Before running ANY Bash command that searches code or text — FIRST call `search` with a natural language query.
- Before reading, editing, or answering questions about ANY file or codebase topic: call `search` first.
- Use ask(scope="global") for holistic questions about the entire codebase.
- Use graph(relation="impact_narrative") for human-readable blast radius analysis.
- In your final answer, reference specific file paths and identifiers found in search results.
- Do NOT delegate codebase questions to sub-agents via the Agent tool.
- NEVER auto-index. Only call `build` when the user explicitly asks.
- If not indexed, say so and ask before indexing.
- After indexing, the daemon watches files automatically.
<!-- <<< opencode-search global instructions <<< -->
