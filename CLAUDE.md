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
.venv/bin/pytest src/tests/ -m "not (gpu or runtime_deps or large or embedder or indexer or slow)" -q

# Full test suite (all non-GPU)
.venv/bin/pytest src/tests/ -m "not (gpu or embedder or indexer)" -q

# MCP contracts + invariants only
.venv/bin/pytest src/tests/integration/test_mcp.py src/tests/integration/test_invariants.py -v

# Code quality
ruff check src/opencode_search src/tests
python -m compileall -q src/opencode_search
```

**CI**: `.github/workflows/ci.yml` — runs on every push (quality → tests → contracts → property tests)

## Project quick reference

- Entry points: `src/opencode_search/mcp.py` (MCP server), `src/opencode_search/handlers/` (tool handlers), `src/opencode_search/daemon.py` (singleton daemon + installer), `src/opencode_search/cli.py` (CLI)
- Registry: `~/.local/share/opencode-search/projects.json`
- Tests: `src/tests/` — `unit/`, `integration/`, `e2e/` subdirs
- LLM provider: codex with gpt-5.4-mini (set in `src/opencode_search/config.py` and `~/.bash_aliases`)
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
