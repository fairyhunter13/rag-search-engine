# rag-search-engine (RSE)

Semantic code-search, knowledge-graph, and AI-assistant integration via a 5-tool MCP API
(search / graph / overview / index). Backed by GPU-accelerated embeddings, a GPU
reranker, and a deterministic tree-sitter call-graph store.

**No generative LLM runs anywhere in this pipeline.** The generative knowledge-base tier
(DeepSeek enrichment, wiki, BPRE, docgen, OKF) was deleted on 2026-07-28; the only LLM the
system reaches at all is `claude -p` on the dashboard's chat route.

## Platform requirements

| | Minimum |
|---|---|
| OS | Linux x86\_64 (systemd user-service) |
| Python | 3.11 or 3.12 |
| GPU | **NVIDIA CUDA GPU** (required — CPU inference is unsupported by design) |
| CUDA | 12.x + matching cuDNN |
| RAM | 8 GB system; 8 GB VRAM recommended |

> **TensorRT:** RSE defaults to `RSE_DISABLE_TENSORRT=1` (uses the CUDA EP, which works on
> every NVIDIA GPU). If your GPU has a compatible TensorRT installation, set
> `RSE_DISABLE_TENSORRT=0` in `~/.config/rag-search/env` to activate the TensorRT EP
> for faster inference.

## Install

```bash
git clone https://github.com/fairyhunter13/rag-search-engine.git
cd rag-search-engine

python3 -m venv .venv
.venv/bin/pip install -e src         # editable install (latest deps)
# Or install from the pinned lock file for a reproducible environment:
# .venv/bin/pip install -r requirements-lock-py312-linux-gpu.txt && .venv/bin/pip install -e src --no-deps
.venv/bin/python scripts/check_system.py          # must show [x] assert_gpu_available()
```

> This repo has **no git submodules** — `vendor/docgen` was deleted with docgen (2026-07-28),
> so a plain `git clone` is complete.

## Configure secrets

**None required.** RSE needs no API key: indexing, extraction and retrieval are entirely
local (GPU embedder + GPU reranker + tree-sitter). The cloud key this section used to
document went with the generative KB tier on 2026-07-28. `~/.config/rag-search/env` is
still read via the systemd unit's `EnvironmentFile` for tuning vars such as
`RSE_DISABLE_TENSORRT`; it just holds no secret any more.

## Run the daemon

```bash
rag-search daemon install-systemd
systemctl --user daemon-reload
systemctl --user enable --now rag-search-mcp-daemon
rag-search daemon status           # → UP — 127.0.0.1:8765
```

## Register with Claude Code (MCP)

MCP server *definitions* live only in `.claude.json` (user/local scope) — `settings.json`
holds MCP *approval* keys, never definitions, so an entry written there is invisible to
Claude Code. `install-global` and `configure_integrations.py` both register via the
official `claude mcp add` CLI against every discovered profile's `.claude.json`.

```bash
rag-search daemon install-global   # registers rag-search in every Claude Code profile
# equivalent, or for multi-profile / Hermes explicitly:
.venv/bin/python scripts/configure_integrations.py --apply-all
.venv/bin/python scripts/configure_integrations.py --check   # verify
```

## Index a project

```bash
rag-search-index /path/to/project      # one-shot: register + index + derive the graph
# or step-by-step:
rag-search init /path/to/project
rag-search index /path/to/project
```

## MCP tool reference

| Tool | Purpose |
|---|---|
| `search` | Semantic code/docs search with GPU-reranked results |
| `ask` | Assemble architecture context for a codebase question |
| `graph` | Call-graph analysis (definition / callers / callees / impact) |
| `overview` | Project metrics, structure, communities, index state |
| `index` | Register or remove a project |

## Verify health

```bash
.venv/bin/python scripts/check_system.py                    # GPU + deps + daemon
.venv/bin/python scripts/configure_integrations.py --check  # MCP wiring
.venv/bin/python scripts/check_world_model.py               # architecture invariants (GPU-free)
```

All three should exit 0 with no `[ ]` failures.

## Health monitoring

`journalctl --user -u rag-search-mcp-daemon` and `systemctl --user status` are the supervision
story. A `health-supervisor.sh` sidecar was deleted in July 2026: it looked for a `rag-search
watch` process, but the daemon's cmdline is `rag-search daemon serve`, so its check returned false
for its entire life — 161k identical `watcher=false` log lines, a false "Watcher Down" desktop
alert 5 minutes after every start, and 99 crash-evidence directories that contain no service log
because the file they read (`~/.local/state/rag-search/rag-search.log`) never existed.

## Codebase layout

```
src/rag_search/
  core/    config, registry, GPU enforcement
  embed/   FastEmbed/ONNX GPU embedder + reranker
  index/   file discovery, chunking, sqlite-vec store
  graph/   tree-sitter symbols, call edges, fastgreedy communities
  kb/      answer_cache (deterministic; all that survives the 2026-07-28 tier-3 deletion)
  query/   search, ask, reranking pipeline
  server/  MCP tools, HTTP routes, dashboard
  daemon/  server lifecycle, watcher, sweeps, systemd, federation
scripts/   check_system.py, configure_integrations.py, check_world_model.py
docs/      architecture, world-model, info-hierarchy, conformance
```

## Architecture docs

- `docs/architecture/federation-and-search-engine.md`
- `docs/architecture/federation-ops-and-invariants.md`
- `docs/world-model/model.yaml` — governing laws P0–P18, requirements HR1–HR40 (both registers
  keep the ids of retired entries, so the numbering never gets reused)
- `docs/info-hierarchy.md` — the DIKW doctrine, now covering the deterministic layers only

## License

MIT — see [`LICENSE`](LICENSE).
