# opencode-search — Install & Setup Guide

GPU-accelerated local semantic code search with MCP integration for AI assistants.

## Requirements

- **NVIDIA GPU** with CUDA support (RTX 20-series or newer recommended; 8 GB+ VRAM)
- **NVIDIA driver** ≥ 550 (check with `nvidia-smi`)
- **Python ≥ 3.11** (Python 3.13 tested)
- **Linux** (Ubuntu / Fedora / Arch — primary support); macOS/Windows untested
- **~6 GB disk** for model downloads (auto-downloaded on first use)

**Hard requirement:** opencode-search refuses to start without CUDA — there is no CPU fallback path. `CPUExecutionProvider` is forbidden by design.

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/fairyhunter13/opencode-search-engine.git
cd opencode-search-engine
```

### 2. Create a Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
```

### 3. Install the package

```bash
pip install -e "src/[dev]"
```

The `[dev]` extras include `pytest`, `ruff`, and `mypy`. Omit them for a leaner runtime install.

For the canonical local developer workflow, see [DEVELOPMENT.md](/home/user/git/github.com/fairyhunter13/opencode-search-engine/DEVELOPMENT.md).

### 4. Verify GPU is detected

```bash
opencode-search health
```

Expected output:

```
GPU:        OK
Provider:   cuda
LanceDB:    0.30.x
FastEmbed:  0.7.x
Python:     3.13.12
```

If `Provider: cpu` or `GPU: FAIL`, check that:

- Both `onnxruntime` (CPU) and `onnxruntime-gpu` are NOT installed simultaneously. Run `pip uninstall onnxruntime` if so.
- `nvidia-smi` works on your host.

## Usage

### CLI

```bash
# Index a project (downloads ~150 MB model on first run)
opencode-search index ~/myproject --tier balanced

# Search across indexed projects
opencode-search search "user authentication" --top 10

# Live-watch a project for incremental re-indexing
opencode-search watch ~/myproject

# Status & listing
opencode-search status ~/myproject
opencode-search list
opencode-search stop-watching ~/myproject

# Health check
opencode-search health --json
```

### Tiers

| Tier      | Embed model                              | Rerank model                              | Dims | Best for           |
|-----------|------------------------------------------|-------------------------------------------|------|--------------------|
| `budget`  | jina-embeddings-v2-small-en              | ms-marco-MiniLM-L-6-v2                    | 512  | Quick, low VRAM    |
| `balanced` (default) | jina-embeddings-v2-base-en    | jina-reranker-v1-turbo-en                 | 768  | Best speed/quality |
| `premium` | jina-embeddings-v2-base-code             | jina-reranker-v2-base-multilingual        | 768  | Code-specific      |

### MCP integration (AI assistants)

The MCP server exposes five tools to AI assistants:

- `index_project(path, tier, watch, force)` — index a directory
- `search_code(query, project_paths, top_k, use_rerank)` — search
- `project_status(path)` — get indexing status
- `list_indexed_projects()` — enumerate projects
- `stop_watching(path)` — stop file-watcher

#### Workspace Scoping (Important)

For Claude Code/Codex/Hermes, you should run the MCP **stdio bridge** (the default in the provided configs). The bridge enforces a workspace boundary:

- By default, it will only index/search/list projects under the opened workspace root.
- If a model tries to pass `project_paths` outside the workspace, the bridge returns an error.
- To intentionally operate outside the workspace (not recommended), set `OPENCODE_ALLOW_INDEX_OUTSIDE_CWD=1`.

To make the boundary unambiguous (and robust to mis-set working directories), set `OPENCODE_BRIDGE_WORKSPACE_ROOT` to the project root when launching the bridge.

#### Global Singleton Daemon

For a shared MCP daemon across Claude Code, Codex, and Hermes:

```bash
.venv/bin/python -m opencode_search daemon install-global
.venv/bin/python -m opencode_search daemon status
```

This does three things:

- registers a stdio MCP bridge in Claude Code, Codex, and Hermes
- installs and enables a user `systemd` service for login-time startup
- configures the bridge so client MCP load auto-starts the singleton daemon if it is not already running

The daemon commands are:

- `opencode-search daemon ensure` — start if needed
- `opencode-search daemon status` — inspect status
- `opencode-search daemon stop` — stop the shared daemon
- `opencode-search daemon serve` — run the HTTP MCP daemon in the foreground
- `opencode-search daemon bridge-stdio` — run the MCP stdio bridge
- `opencode-search daemon install-systemd` — install the login-time user service

The daemon also supports:

- idle auto-shutdown after `OPENCODE_MCP_IDLE_SHUTDOWN_S` seconds with no active MCP clients
- client-session tracking through the stdio bridge with heartbeat expiry
- login-time startup through `systemd --user`

#### Claude Code

Add the stdio bridge as the MCP server:

```json
{
  "mcpServers": {
    "opencode-search": {
      "command": "opencode-search",
      "args": ["daemon", "bridge-stdio"]
    }
  }
}
```

A pre-made config is at `mcp-config/claude-code.json`.

#### Codex CLI

See `mcp-config/codex.json`. Wire its contents into your Codex MCP registry.

#### Hermes

See `mcp-config/hermes.json`.

## Environment variables

| Variable                              | Default                     | Meaning                                         |
|---------------------------------------|-----------------------------|-------------------------------------------------|
| `OPENCODE_BRIDGE_WORKSPACE_ROOT`      | *(unset)*                   | Pin the MCP stdio bridge workspace root. When set, the bridge will refuse to index/search projects outside this directory (unless `OPENCODE_ALLOW_INDEX_OUTSIDE_CWD=1`). |
| `OPENCODE_REGISTRY_PATH`              | `~/.local/share/opencode-search/projects.json` | Where the project registry is persisted         |
| `OPENCODE_INDEX_ROOT`                 | `~/.local/share/opencode-search/indexes`       | Centralized root directory for LanceDB indexes  |
| `OPENCODE_DEBOUNCE_DELAY_MS`          | `1000`                      | Watcher debounce window                         |
| `OPENCODE_MIN_FLUSH_INTERVAL_S`       | `5`                         | Min seconds between watcher flushes             |
| `OPENCODE_STAGE1_VECTOR_K`            | `20`                        | Per-project vector candidates                   |
| `OPENCODE_STAGE1_RERANK_K`            | `15`                        | Per-project rerank top-k                        |
| `OPENCODE_GLOBAL_RERANK_MAX`          | `100`                       | Max candidates before global rerank             |
| `OPENCODE_FINAL_TOP_K`                | `10`                        | Default `top_k` for `search_code`               |
| `OPENCODE_SKIP_STAGE1_RERANK_N`       | `5`                         | Skip per-project rerank above this many projects|
| `OPENCODE_ONNX_LOG_SEVERITY`          | `3`                         | ONNX Runtime log level (`3` hides warnings)     |
| `OPENCODE_RERANK_NORMALIZE`           | `sigmoid`                   | `sigmoid` or `minmax`                           |
| `OPENCODE_RERANKER_CACHE_SIZE`        | `2`                         | LRU model cache for cross-encoders              |
| `OPENCODE_SEARCH_CACHE_SIZE`          | `128`                       | Query result cache size (TTL'd)                 |
| `OPENCODE_SEARCH_CACHE_TTL`           | `60`                        | Query result cache TTL (seconds)                |

## Troubleshooting

### `GPUNotAvailableError` at startup

You're missing the CUDA execution provider. Run:

```bash
pip uninstall onnxruntime  # remove the CPU package if present
pip install --force-reinstall "onnxruntime-gpu[cuda,cudnn]>=1.24.0"
opencode-search health
```

### `ModuleNotFoundError` for a runtime package

This usually means the environment was installed before the dependency list was updated, or the install was partial. Reinstall the package set:

```bash
pip install --upgrade -e "src/[dev]"
```

### Slow first search

The reranker model (~150 MB) downloads on the first search. Subsequent calls are cached on disk in `~/.cache/huggingface/`.

### Watcher misses changes

The watcher uses `inotify` on Linux with a 1-second debounce. If you bulk-edit thousands of files, the inotify queue can overflow:

```bash
echo "fs.inotify.max_user_watches=524288" | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

## Running tests

```bash
./scripts/validate-local-gpu.sh
```

That local validation path is strict:

- it fails immediately if CUDA is unavailable
- it fails if core runtime packages are missing
- it fails if any test is skipped
- it runs lint, bytecode compilation, the Python test suite, and the CLI end-to-end smoke flow locally

Ad-hoc `pytest` runs are dependency-aware: integration tests may skip if runtime packages such as `lancedb`, `pyarrow`, or `typer` are not installed in the current environment.

## Dependency lock

This repo keeps a Python 3.12 Linux GPU lock snapshot at `requirements-lock-py312-linux-gpu.txt`.
Refresh it only from the repo-local `.venv`:

```bash
./scripts/refresh-lock.sh
```

## Reindexing and migrations

Force a rebuild after changing schema, chunking, language detection, embedding models, embedding dimensions, or tier:

```bash
opencode-search index ~/myproject --tier balanced --force
```

If an index needs a fully clean rebuild, remove the centralized index directory and index again:

```bash
rm -rf ~/.local/share/opencode-search/indexes/<project-slug>-<hash>/index_balanced
opencode-search index ~/myproject --tier balanced --force
```

Mixed-tier federated search is rejected by design because the underlying embedding models and dimensions can differ. Reindex projects to the same tier before searching them together.

## Architecture overview

```
┌─────────────────────────────────────────────────────────────┐
│   Claude Code / Codex / any MCP-compatible AI assistant     │
└──────────────────────────┬──────────────────────────────────┘
                           │ stdio (JSON-RPC)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│   opencode-search mcp  ──  FastMCP stdio server             │
│   ├── 5 tools (index_project, search_code, …)               │
│   ├── GPU guard at startup (CPU forbidden)                  │
│   └── Watcher resume from registry                          │
└──────────────────────────┬──────────────────────────────────┘
                           │
        ┌──────────────────┼───────────────────┐
        ▼                  ▼                   ▼
┌──────────────┐   ┌──────────────┐   ┌──────────────────┐
│  Indexer     │   │   Search     │   │   Watcher        │
│  (chunk →    │   │  (hybrid +   │   │  (watchdog +     │
│   embed →    │   │   2-stage    │   │   debounce →     │
│   store)     │   │   rerank)    │   │   incremental)   │
└──────┬───────┘   └──────┬───────┘   └──────┬───────────┘
       │                  │                  │
       └─────────┬────────┴───────────┬──────┘
                 ▼                    ▼
         ┌───────────────┐   ┌────────────────┐
         │   LanceDB     │   │   ONNX Runtime │
         │ (centralized  │   │   CUDA GPU     │
         │  vector DBs)  │   │   (mandatory)  │
         └───────────────┘   └────────────────┘
```

Per-project DB lives at `~/.local/share/opencode-search/indexes/<project-slug>-<hash>/index_{tier}/` by default.
The cross-project registry lives at `~/.local/share/opencode-search/projects.json`.
Existing legacy registry entries are migrated from `<project>/.opencode/index_{tier}/` on load.
