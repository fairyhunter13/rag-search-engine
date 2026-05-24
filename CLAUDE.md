# opencode-search-engine — Claude Code Instructions

This project is indexed in **opencode-search**. You MUST use the MCP tools before any native file exploration.

## Mandatory tool-use protocol (no exceptions)

1. **Call `list_indexed_projects` first** — before reading any file, grepping, globbing, or answering any question about this codebase.
2. **Call `search_code` immediately after** — use a natural-language query describing what you need to find.
3. Only fall back to Bash/Grep/Glob/Read if `search_code` returns no useful results.

This rule applies to EVERY codebase question, including questions that seem simple or questions you think you already know the answer to from training data. Training-data answers about this codebase are stale and untrustworthy.

## Why this matters

opencode-search is the tool under test. Every call to `list_indexed_projects` and `search_code` validates that the MCP toolchain works end-to-end. Skipping them defeats the purpose of this project.

## Project quick reference

- Entry points: `src/opencode_search/mcp.py` (MCP server), `src/opencode_search/handlers.py` (tool handlers), `src/opencode_search/daemon.py` (singleton daemon + installer), `src/opencode_search/cli.py` (CLI)
- Registry: `~/.local/share/opencode-search/projects.json`
- Tests: `src/tests/` — run with `.venv/bin/pytest src/tests/`
- Benchmark: `scripts/benchmark_mcp.py`
