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
.venv/bin/pytest src/tests/live/ -m "live and not slow" --ignore=src/tests/live/test_browser.py -q

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

- Entry points: `src/opencode_search/server/mcp.py` (MCP server + routes), `src/opencode_search/daemon/` (daemon package), `src/opencode_search/cli.py` (CLI), `src/opencode_search/__main__.py` (bridge-stdio shim)
- Packages: `core/ embed/ index/ graph/ kb/ query/ server/ daemon/` under `src/opencode_search/`
- Registry: `~/.local/share/opencode-search/projects.json`
- Tests: `src/tests/live/` (live suite — requires daemon at :8765, Ollama, GPU)
- LLM provider: ollama + qwen3-enrich:1.7b (GPU-local, RTX 5080; systemd service; see `scripts/modelfiles/qwen3-enrich.modelfile`)
- Setup scripts: `scripts/configure_integrations.py`, `scripts/check_system.py`
