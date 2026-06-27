# opencode-search-engine — Claude Code Instructions

Indexed in opencode-search — global MCP doctrine lives in `~/.claude/CLAUDE.md`; project protocol below.

## Mandatory tool-use protocol (no exceptions)

1. **Call `overview(what='projects')` first** — confirm the project is indexed before doing anything else.
2. **Call `search` immediately after** — use a natural-language query describing what you need to find.
3. Do NOT delegate to sub-agents via the `Agent` tool — sub-agents do not inherit these instructions. Answer directly.
4. Only fall back to Bash/Grep/Glob/Read if `search` returns no useful results.

This rule applies to EVERY codebase question, even ones that seem simple. Training-data answers about this codebase are stale and untrustworthy.

## Running tests and quality checks

```bash
# Fast smoke check — skips LLM quality tests + browser tests (~5 min)
.venv/bin/pytest src/tests/live/ -m "live and not slow" --ignore=src/tests/live/test_browser.py -x --strict-markers --strict-config -ra -q

# Full live suite — all intents, quality scoring, watcher (~40 min, no browser)
.venv/bin/pytest src/tests/live/ --ignore=src/tests/live/test_browser.py -x --strict-markers --strict-config -ra -q

# Browser / Playwright tests (run separately — conflicts with pytest-asyncio mode=auto)
.venv/bin/pytest src/tests/live/test_browser.py -v --browser chromium

# Code quality
ruff check src/opencode_search src/tests
python -m compileall -q src/opencode_search
```

**Test markers**:
- `live` — requires daemon at :8765, GPU
- `slow` — LLM-heavy tests (>30s each); skip with `-m "live and not slow"` for fast feedback

**Memory profile**: the live suite loads a real embedder in-process (~1 GB) — intrinsic to the no-mock invariant. Use the fast smoke command above as the default to keep peak RSS lower. Browser tests run in a separate process; don't run them together with the live suite.

**IMPORTANT — run tests foreground only**: never leave the live suite as an unattended background task. The in-process embedder (~1 GB RSS + a full CPU core) stacks on Chrome/Java/Node and can push the machine into swap, freezing the UI. Run pytest in the foreground, serialized, when other heavy apps are not contending.

**Daemon reload** (after code changes): `POST /api/reload` or `systemctl --user restart opencode-search-mcp-daemon` — daemon restarts via systemd in ~1s. (There is no `daemon reload` CLI subcommand; only `daemon serve/status/ensure/stop/install-global/install-systemd/bridge-stdio` exist.)

**Stream error metrics**: `overview(what="metrics")` returns `chat_stream.stream_error_count` and `chat_stream.error_by_intent`.

**Key env vars** (BPRE resolution ladder):
- `OSE_DEEPSEEK_MODEL` — override DeepSeek model (default `deepseek-v4-flash`; `deepseek-chat` alias deprecates 2026-07-24)
- All LLM lanes (Tier-2 edge linkage, Tier-3 whole-file, BPRE narrative, wiki L2) are **ON by default**, suppressed only when `DEEPSEEK_API_KEY` is absent.

**CI**: `.github/workflows/ci.yml` — runs on every push (quality → tests → contracts → property tests)

## GPU-only enforcement

**CPU fallback is forbidden.** All inference (embeddings + LLMs) runs on GPU (NVIDIA CUDA).
Any CPU fallback must raise a fatal error — never fall back silently.

## Project quick reference

- Entry points: `src/opencode_search/server/mcp.py` (MCP server + routes), `src/opencode_search/daemon/` (daemon package), `src/opencode_search/cli.py` (CLI), `src/opencode_search/__main__.py` (bridge-stdio shim)
- Packages: `core/ embed/ index/ graph/ kb/ query/ server/ daemon/` under `src/opencode_search/`
- Registry: `~/.local/share/opencode-search/projects.json`
- Tests: `src/tests/live/` (live suite — requires daemon at :8765, GPU; no local generative LLM)
- LLM: GPU = FastEmbed/ONNX/CUDA (embeddings + reranking only); KB build = cloud DeepSeek; chat = claude-haiku-4-5 + DeepSeek fallback
- Setup scripts: `scripts/configure_integrations.py`, `scripts/check_system.py`
- Architecture: `docs/architecture/federation-and-search-engine.md` + `docs/architecture/federation-ops-and-invariants.md`

## World model & info-hierarchy

The OSE world model (governing laws, component map, behavior specs) lives in `docs/world-model/`.
The DIKW doctrine ladder lives in `docs/info-hierarchy.md`.
Generated Claude Code skills: `.claude/skills/world-model.md` + `.claude/skills/info-hierarchy.md`.

```bash
# Check working-tree conformance (GPU-free, daemon-free):
python scripts/check_world_model.py

# Regenerate skills after editing model.yaml or info-hierarchy.md:
python scripts/gen_world_model_skills.py
```
