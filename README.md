# rag-search-engine (OSE)

Semantic code-search, knowledge-graph, and AI-assistant integration via a 5-tool MCP API
(search / ask / graph / overview / index). Backed by GPU-accelerated embeddings, a
call-graph store, and a DeepSeek-powered knowledge-base pipeline.

## Platform requirements

| | Minimum |
|---|---|
| OS | Linux x86\_64 (systemd user-service) |
| Python | 3.11 or 3.12 |
| GPU | **NVIDIA CUDA GPU** (required — CPU inference is unsupported by design) |
| CUDA | 12.x + matching cuDNN |
| RAM | 8 GB system; 8 GB VRAM recommended |

> **TensorRT:** OSE defaults to `OPENCODE_DISABLE_TENSORRT=1` (uses the CUDA EP, which works on
> every NVIDIA GPU). If your GPU has a compatible TensorRT installation, set
> `OPENCODE_DISABLE_TENSORRT=0` in `~/.config/rag-search/env` to activate the TensorRT EP
> for faster inference.

## Install

```bash
git clone --recurse-submodules https://github.com/fairyhunter13/rag-search-engine.git
cd rag-search-engine
# If you cloned without --recurse-submodules, run this to populate vendor/docgen:
git submodule update --init --recursive

python3 -m venv .venv
.venv/bin/pip install -e src         # editable install (latest deps)
# Or install from the pinned lock file for a reproducible environment:
# .venv/bin/pip install -r requirements-lock-py312-linux-gpu.txt && .venv/bin/pip install -e src --no-deps
.venv/bin/python scripts/check_system.py          # must show [x] assert_gpu_available()
```

> **`rag-search docgen`** requires the `vendor/docgen` submodule.
> All other features (`ocs-index`, search, chat) work without it.

## Configure secrets

OSE needs a [DeepSeek](https://platform.deepseek.com/) API key for KB-enrichment.

Resolution order: `DEEPSEEK_API_KEY` env var → `~/.config/rag-search/env` → (legacy `~/.bash_env`)

```bash
mkdir -p ~/.config/rag-search
echo "DEEPSEEK_API_KEY=sk-..." >> ~/.config/rag-search/env
chmod 600 ~/.config/rag-search/env
```

The systemd unit reads this file automatically via `EnvironmentFile`.

## Run the daemon

```bash
rag-search daemon install-systemd
systemctl --user daemon-reload
systemctl --user enable --now rag-search-mcp-daemon
rag-search daemon status           # → UP — 127.0.0.1:8765
```

## Register with Claude Code (MCP)

```bash
rag-search daemon install-global   # writes ~/.claude.json entry
# multi-profile / Hermes:
.venv/bin/python scripts/configure_integrations.py --apply-all
.venv/bin/python scripts/configure_integrations.py --check   # verify
```

## Index a project

```bash
ocs-index /path/to/project             # one-shot: register + index + enrich + wiki
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
| `overview` | Project metrics, structure, communities, KB state |
| `index` | Register or remove a project |

## Verify health

```bash
.venv/bin/python scripts/check_system.py                    # GPU + deps + daemon + LLM
.venv/bin/python scripts/configure_integrations.py --check  # MCP wiring
python scripts/check_world_model.py               # architecture invariants (GPU-free)
```

All three should exit 0 with no `[ ]` failures.

## Health supervisor

`health-supervisor.sh` captures crash evidence to `~/.local/state/rag-search/health/crashes/` and sends
desktop notifications on fatal failure. Optional; does not replace systemd.

## Codebase layout

```
src/rag_search/
  core/    config, registry, GPU enforcement
  embed/   FastEmbed/ONNX GPU embedder + reranker
  index/   file discovery, chunking, sqlite-vec store
  graph/   tree-sitter symbols, call edges, Leiden communities
  kb/      DeepSeek KB enrichment, wiki, BPRE, docgen, OKF
  query/   search, ask, reranking pipeline
  server/  MCP tools, HTTP routes, dashboard
  daemon/  server lifecycle, watcher, sweeps, systemd, federation
vendor/    ose-docgen, okf (git submodules)
scripts/   check_system.py, configure_integrations.py, check_world_model.py
docs/      architecture, world-model, info-hierarchy, conformance
```

## Architecture docs

- `docs/architecture/federation-and-search-engine.md`
- `docs/architecture/federation-ops-and-invariants.md`
- `docs/world-model/model.yaml` — governing laws P0–P15, requirements HR1–HR31
- `docs/info-hierarchy.md` — DIKW doctrine for KB artefacts
