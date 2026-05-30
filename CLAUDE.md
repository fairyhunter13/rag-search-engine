# opencode-search-engine — Claude Code Instructions

This project is indexed in **opencode-search**. You MUST use the MCP tools before any native file exploration.

## Mandatory tool-use protocol (no exceptions)

1. **Call `list_indexed_projects` first** — before reading any file, grepping, globbing, or answering any question about this codebase.
2. **Call `search_code` immediately after** — use a natural-language query describing what you need to find.
3. Do NOT delegate to sub-agents via the `Agent` tool — sub-agents do not inherit these instructions. Answer directly.
4. Only fall back to Bash/Grep/Glob/Read if `search_code` returns no useful results.

This rule applies to EVERY codebase question, including questions that seem simple or questions you think you already know the answer to from training data. Training-data answers about this codebase are stale and untrustworthy.

## Why this matters

opencode-search is the tool under test. Every call to `list_indexed_projects` and `search_code` validates that the MCP toolchain works end-to-end. Skipping them defeats the purpose of this project.

## Project quick reference

- Entry points: `src/opencode_search/mcp.py` (MCP server), `src/opencode_search/handlers.py` (tool handlers), `src/opencode_search/daemon.py` (singleton daemon + installer), `src/opencode_search/cli.py` (CLI)
- Registry: `~/.local/share/opencode-search/projects.json`
- Tests: `src/tests/` — run with `.venv/bin/pytest src/tests/`
- Benchmark: `scripts/benchmark_mcp.py`

[opencode-search-global-instructions:start]
MANDATORY: Use the opencode-search MCP server as the primary code lookup tool whenever the current project is indexed.

7-tool intent API — pick the right tool:
- `search(query, scope, project_paths)` — find SPECIFIC code/files/functions. scope: "code" (default)|"docs"|"all"
- `ask(query, project_path, scope)` — 'how does X work?', architecture, business process. scope: "all" (default)|"architecture"|"wiki"
- `graph(symbol, project_path, relation)` — callers, callees, impact, call path. relation: "definition"|"callers"|"callees"|"impact"|"path"
- `overview(project_path, what)` — structure, communities, status, project list, metrics. what: "structure"|"communities"|"status"|"projects"|"metrics"
- `build(project_path, action)` — index, pipeline (full KB build), enrich, wiki, ingest docs. action: "pipeline" (default, recommended first-run)
- `federation(root_path, action)` — discover/list/add/remove/index federation sub-repos
- `manage(project_path, action)` — stop_watching, wiki_lint

Rules (no exceptions):
- Before running ANY Bash command that searches code or text — grep, rg, ag, find -name/-exec, glob, fd, or similar — FIRST call `search` with a natural language query. Only fall back to bash search commands if `search` returns no useful results or the project is not indexed.
- Before reading, editing, or answering questions about ANY file or codebase topic: call `search` first. For architectural questions use `ask`. Do NOT go straight to Bash/grep/find/Read for codebase exploration.
- When answering a user question, prefer using the user's question text verbatim as the initial query. For 'how does X work?' questions use `ask`; for finding specific code use `search`.
- In your final answer, reference specific file paths and identifiers found in search results so the answer is grounded and unambiguous.
- Do NOT delegate codebase questions to sub-agents via the Agent tool — sub-agents do not inherit these instructions. Call the tools yourself, directly.
- Never auto-index a project. Only call `build(action="index")` or `build(action="pipeline")` when the user explicitly asks to index/setup the project.
- If the current project is not indexed and the user did not explicitly ask to index it, say that the project is not indexed yet and ask before indexing.
- After a project has been explicitly indexed, rely on the daemon's automatic watch behavior while the client remains open.
[opencode-search-global-instructions:end]
