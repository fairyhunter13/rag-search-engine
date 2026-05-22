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

### 4. Install the `openai` package (chonkie compat shim)

```bash
pip install openai
```

Chonkie's embedding registry eagerly imports `openai`. We don't actually use OpenAI's API — this is just a stub to satisfy the import.

### 5. Verify GPU is detected

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

The MCP server runs via stdio and exposes five tools to AI assistants:

- `index_project(path, tier, watch, force)` — index a directory
- `search_code(query, project_paths, top_k, use_rerank)` — search
- `project_status(path)` — get indexing status
- `list_indexed_projects()` — enumerate projects
- `stop_watching(path)` — stop file-watcher

#### Claude Code

Add to `~/.config/claude-code/mcp.json` (or the OS-specific equivalent):

```json
{
  "mcpServers": {
    "opencode-search": {
      "command": "opencode-search",
      "args": ["mcp"]
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
| `OPENCODE_REGISTRY_PATH`              | `~/.opencode/projects.json` | Where the project registry is persisted         |
| `OPENCODE_DEBOUNCE_DELAY_MS`          | `1000`                      | Watcher debounce window                         |
| `OPENCODE_MIN_FLUSH_INTERVAL_S`       | `5`                         | Min seconds between watcher flushes             |
| `OPENCODE_STAGE1_VECTOR_K`            | `20`                        | Per-project vector candidates                   |
| `OPENCODE_STAGE1_RERANK_K`            | `15`                        | Per-project rerank top-k                        |
| `OPENCODE_GLOBAL_RERANK_MAX`          | `100`                       | Max candidates before global rerank             |
| `OPENCODE_FINAL_TOP_K`                | `10`                        | Default `top_k` for `search_code`               |
| `OPENCODE_SKIP_STAGE1_RERANK_N`       | `5`                         | Skip per-project rerank above this many projects|
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

### `ModuleNotFoundError: No module named 'openai'`

Chonkie's import chain expects the `openai` package. Install it:

```bash
pip install openai
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
cd src
pytest tests/
```

232 tests should pass on a non-GPU machine (GPU-only tests are marked `@pytest.mark.gpu` and auto-skip without CUDA). On a GPU machine, no tests are skipped.

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
         │  (per-project │   │   CUDA GPU     │
         │   vector DB)  │   │   (mandatory)  │
         └───────────────┘   └────────────────┘
```

Per-project DB lives at `{project_root}/.opencode/index_{tier}/`.
The cross-project registry lives at `~/.opencode/projects.json`.
