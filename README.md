# opencode-search

GPU-accelerated semantic code intelligence with MCP integration for AI assistants (Claude Code, Codex, Hermes).

## What is opencode-search?

opencode-search is a local MCP server that indexes your codebases on an NVIDIA GPU and exposes a
7-tool API that AI assistants can query in natural language. It supports semantic code search,
architectural reasoning, call-graph analysis, wiki generation, and background KB enrichment —
all running locally on your GPU with no data leaving your machine.

**Hard requirement:** CUDA GPU (RTX 20-series or newer). CPU fallback is intentionally disabled.

## Quick start

See **[docs/INSTALL.md](docs/INSTALL.md)** for full installation and setup instructions.

Tool reference: **[CLAUDE.md](CLAUDE.md)** documents all 7 tools, quick-decision guide, and usage rules.

```bash
# Index a project
opencode-search index ~/myproject --tier balanced

# Start the shared MCP daemon
opencode-search daemon install-global

# Health check
opencode-search health
```

## Project layout

```
src/          Python package (opencode_search) + live tests
scripts/      Developer utilities (check_system.py, setup_llm_services.py, …)
docs/         INSTALL.md (full setup guide)
mcp-config/   Ready-made MCP configs for Claude Code, Codex, and Hermes
.github/      CI workflows
```

For the developer workflow, see [DEVELOPMENT.md](DEVELOPMENT.md).
