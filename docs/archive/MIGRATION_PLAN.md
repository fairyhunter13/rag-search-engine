# Migration Plan: Rust + Python → Unified Python Package

## Overview

Merge the Rust indexer (17K lines, 22 files) and Python embedder (4.7K lines) into a single Python package. Eliminate HTTP IPC between components; all ML calls become direct in-process function calls.

## Architecture After Migration

```
Claude Code / Codex / Hermes
        │ MCP (stdio)
        ▼
opencode_search.mcp        ← single entry point, IS the daemon
        │ direct calls (no HTTP)
        ├── indexer.py     ← discover → chunk → embed → store
        ├── watcher.py     ← watchdog, debounce, incremental re-index
        ├── search.py      ← hybrid vector+FTS, federated, rerank
        ├── storage.py     ← LanceDB + pyarrow
        └── embeddings.py  ← GPU-only ONNX, 6 workers, FP16, batch-256
                │ CUDA
                ▼
           RTX 5080 (16GB VRAM)
```

**Key structural change:** The MCP server *is* the daemon — one process, no IPC between them. `server.py` (HTTP server in the embedder) is deleted entirely.

## New Package Layout

```
src/opencode_search/
    __init__.py
    __main__.py          # python -m opencode_search
    cli.py               # typer CLI
    config.py            # env vars, dataclasses, project registry (~/.opencode/projects.json)
    hardware.py          # GPU/CPU detection, worker count from VRAM
    storage.py           # LanceDB + pyarrow, same schema as today
    discover.py          # file discovery, gitignore (pathspec), language detection
    chunker.py           # moved from embedder
    embeddings.py        # moved + GPU-maximized: FP16, 6 workers, batch-256, CuPy
    tokenizer.py         # moved from embedder
    cuda_setup.py        # moved from embedder
    indexer.py           # full-project + per-file indexing pipeline
    watcher.py           # watchdog file watcher, debounce, incremental updates
    compaction.py        # LanceDB compaction
    cleaner.py           # stale chunk removal
    search.py            # hybrid search, federated, two-stage rerank, LRU cache
    mcp.py               # MCP server (stdio transport), tool definitions, auto-watch
    handlers.py          # RPC method implementations (index, search, status, watcher_*)
pyproject.toml
tests/
    conftest.py
    test_storage.py
    test_discover.py
    test_indexer.py
    test_watcher.py
    test_search.py
    test_reranker.py
    test_mcp.py
    test_gpu.py
    test_e2e.py
```

## Dependency Swaps

| Rust crate | Python replacement |
|---|---|
| `lancedb` (Rust SDK) | `lancedb` (Python SDK) |
| `arrow_array` / `arrow_schema` | `pyarrow` |
| `clap` | `typer` |
| `notify` (inotify) | `watchdog` |
| `axum` (HTTP server) | `fastapi` + `uvicorn` |
| `ignore` (gitignore traversal) | `pathspec` |
| `lru` | `cachetools.LRUCache` |
| `rayon` (parallel iter) | `asyncio.gather` + `ThreadPoolExecutor` |
| SIMD cosine in `simd.rs` | `numpy` (BLAS-optimized) |

---

## Phase 1 — Repo Restructure + Scaffolding

**Goal:** Create the unified package skeleton, move embedder files in, set up unified `pyproject.toml`, delete server.py.

**Steps:**
1. Create `src/opencode_search/` with `__init__.py` and `__main__.py` stub
2. Copy `embedder/opencode_embedder/{chunker,embeddings,tokenizer,cuda_setup}.py` → `src/opencode_search/`, update all internal imports
3. Write `pyproject.toml` combining all embedder deps + new: `typer`, `watchdog`, `fastapi`, `uvicorn[standard]`, `pathspec`, `cachetools`, `mcp` (Anthropic MCP SDK), `pytest`, `pytest-asyncio`
4. Stub out all new module files with `pass` bodies so imports work
5. Delete `embedder/opencode_embedder/server.py`
6. Verify `python -m opencode_search --help` runs without import errors

**Risk:** Low. No logic changes.

---

## Phase 2 — Storage Layer

**Goal:** Port `storage.rs` (3,275 lines) → `storage.py`. Schema must be byte-for-byte compatible with existing Rust-created LanceDB indexes.

**Arrow schema (must match Rust exactly):**
```python
SCHEMA = pa.schema([
    pa.field("chunk_id",     pa.int64()),
    pa.field("path",         pa.utf8()),
    pa.field("file_hash",    pa.utf8()),
    pa.field("language",     pa.utf8()),
    pa.field("position",     pa.int32()),
    pa.field("content",      pa.utf8()),
    pa.field("content_hash", pa.utf8()),
    pa.field("start_line",   pa.int32()),
    pa.field("end_line",     pa.int32()),
    pa.field("vector",       pa.list_(pa.float32(), dims)),
    pa.field("created_at",   pa.timestamp("us")),
])
```

**Steps:**
1. `Storage.open(db_path, dims)` — `lancedb.connect()`, create `chunks`/`config`/`usage` tables if absent, validate schema version
2. `Storage.write_chunks(chunks)` — build `pa.RecordBatch`, upsert via `table.merge_insert("chunk_id")`
3. `Storage.search_hybrid(query_text, query_vec, limit)` — vector search + FTS via lancedb Python query builder
4. `Storage.ensure_fts_index()` — create FTS index when `chunk_count > FTS_THRESHOLD (50)`
5. `Storage.ensure_ivf_pq_index()` — create IVF-PQ when `chunk_count > IVF_PQ_THRESHOLD (512)`; partitions = `clamp(count//10, 1, 256)`, sub_vectors = `clamp(dims//4, 1, 96)`
6. `Storage.get_config/set_config` — config table reads/writes (tier, dims, file_count, schema_version)
7. `compaction.py` — `table.compact_files()` at ops thresholds
8. `cleaner.py` — delete chunks where path not in current file set

**Risk:** Medium — schema compatibility critical. Write compatibility test before any writes.

---

## Phase 3 — File Discovery

**Goal:** Port `discover.rs` (896 lines) → `discover.py`.

**Steps:**
1. Extension and directory ignore lists (copy exactly from Rust: `target/`, `node_modules/`, `.git/`, `__pycache__/`, `vendor/`, `dist/`, `build/`, etc.)
2. `gitignore_walker(root)` — `pathspec.PathSpec.from_lines("gitwildmatch", ...)` at each directory level; stack rules parent → child
3. `detect_language(path)` — extract extension map from `chunker.py` as single source of truth
4. `iter_files(root, follow_symlinks=True)` — `os.walk(root, followlinks=True)`, apply gitignore + ignore-list rules + size limits
   - Source files: 2MB max
   - Text files: 1MB max
   - Unknown: 512KB max
   - Override via `OPENCODE_SEARCH_MAX_FILE_SIZE_KB`
5. Path canonicalization LRU (200 entries, 5-min TTL)

**Risk:** Low.

---

## Phase 4 — GPU-Maximized Embeddings Layer

**Goal:** Enhance `embeddings.py` for maximum GPU utilization. Target: 6 workers, batch-256, FP16 forced, CuPy normalization, CUDA streams, zero CPU fallback.

**Current vs target:**
- Current: 2–3 workers, batch 96, FP16 opportunistic → ~288 tokens/step
- Target: 6 workers, batch 256, FP16 forced → ~1,536 tokens/step (~5–8x throughput)

**Steps:**

**4a — GPU-only enforcement:**
- `providers=["CUDAExecutionProvider"]` with no CPU fallback
- Raise `GPUNotAvailableError` at startup if CUDA unavailable

**4b — Worker count from VRAM:**
```python
workers = min(6, max(2, (vram_mb - 1024) // 600))
# RTX 5080 (16GB): (16303 - 1024) // 600 = 25, clamped to 6
```

**4c — Batch size increase:**
- `EMBED_PASSAGES_MAX_TEXTS`: 96 → 256
- `EMBED_PASSAGES_MAX_BYTES`: 8MB → 24MB

**4d — FP16 forced inference:**
- `ort.GraphOptimizationLevel.ORT_ENABLE_ALL` in session options
- `enable_cuda_graph=True` for SM < 12.0 (disabled on Blackwell per existing code)

**4e — CuPy GPU-resident normalization:**
- After embedding batch, normalize in-place on GPU: `vecs /= cupy.linalg.norm(vecs, axis=1, keepdims=True)`
- Transfer to CPU only when writing to LanceDB

**4f — Async prefetch pipeline:**
- `asyncio.Queue(maxsize=2)` between tokenization (CPU ThreadPool) and embedding (GPU)
- While GPU processes batch N, tokenizer prepares batch N+1

**4g — Pinned memory:**
- Input arrays on page-locked memory via `cupy.cuda.alloc_pinned_memory()`

**Risk:** Medium. FP16 accuracy validation required (cosine similarity within 0.01 of FP32).

---

## Phase 5 — Indexing Pipeline

**Goal:** Port `cli.rs` indexing loop → `indexer.py`.

**Steps:**
1. `async def index_project(root, db_path, tier, force, dims)` — main entry
2. `discover.iter_files(root)` → candidate paths
3. Bulk-load existing `(path, file_hash)` from LanceDB in one query
4. SHA-256 hashing via `ThreadPoolExecutor(10)` — parallel I/O
5. Diff: changed hashes → `to_index`; missing paths → `cleaner`
6. Pipeline per file:
   - Read content (ThreadPool)
   - `chunker.chunk_file(path, content)` — direct call
   - Batch to 256 texts / 24MB → `embeddings.embed_passages(batch, model, dims)` — direct GPU call
   - Write `ChunkData` to `Storage.write_chunks()`
7. `asyncio.Semaphore(embed_concurrency)` — default `min(6, workers)`
8. Post-index: `cleaner.remove_stale_chunks()` + `storage.ensure_fts_index()` + compaction check
9. Progress JSON-lines to stdout: `{"type":"progress","indexed":N,"total":M,"file":"..."}`

**Risk:** Medium — batching + semaphore back-pressure tuning.

---

## Phase 6 — File Watcher

**Goal:** Port `watcher.rs` + `watcher_startup.rs` (1,385 lines) → `watcher.py` using `watchdog`.

**Steps:**
1. `watchdog.observers.Observer` (inotify on Linux) with `FileChangeHandler`
2. Handler pushes `(event_type, path)` into `asyncio.Queue`
3. Debounce coroutine:
   - Accumulate events for `DEBOUNCE_DELAY=1s`
   - Enforce `MIN_FLUSH_INTERVAL=5s` between consecutive flushes
4. On flush:
   - `created`/`modified` → `indexer.index_files(paths, db_path, tier, dims)`
   - `deleted` → `cleaner.remove_chunks_for_paths(storage, paths)` + invalidate query cache
5. Apply gitignore/ignore-list filter from `discover.py` to incoming events
6. `WatcherHandle`: `{observer, debounce_task, root, db_path, last_flush}`
7. `WatcherManager`: dict of `project_key → WatcherHandle`, start/stop per project
8. Graceful shutdown: `observer.stop(); observer.join()` on SIGTERM/SIGINT

**Risk:** Medium — debounce + rate limiting nuanced. Test with `git checkout` simulation.

---

## Phase 7 — Search

**Goal:** Port `search.rs` (681 lines) → `search.py`. All ML calls are direct function calls.

**Steps:**
1. `cachetools.TTLCache(maxsize=50, ttl=30)` keyed by `(query.lower().strip(), db_path, tier, dims, tuple(sorted(federated)))`
2. `async def search(root, db, query, tier, dims, federated, mounts)` — main entry
3. Single-project: `embeddings.embed_query()` → `Storage.search_hybrid()` → `embeddings.rerank()`
4. Federated: `asyncio.gather` with `asyncio.Semaphore(min(cpu_count, 4))`
5. Adaptive strategy:
   - `≤5 projects`: two-stage (per-project rerank k=15, global rerank k=10)
   - `>5 projects`: vector-only + global rerank (speed)
6. Deduplication by path (keep highest score) before global rerank
7. `invalidate_query_cache(db_path)` called by watcher after any write
8. Port `search_memories_impl`, `search_activity_impl`, `search_skills_impl`

**Risk:** Low-medium.

---

## Phase 8 — MCP Server (absorbs daemon)

**Goal:** The MCP server is the process — handles tools, RPC, watcher state, and the primary interface for all AI assistants.

### 8a — MCP server core (`mcp.py`)
- `mcp = FastMCP("opencode-search")` using Anthropic MCP Python SDK
- stdio transport — spawned by each AI assistant
- Startup: read project registry (`~/.opencode/projects.json`); if `MCP_ROOTS` set, check each root → auto-start watcher

### 8b — MCP tools
```python
@mcp.tool()
async def index_project(path: str = ".", tier: str = "premium", force: bool = False) -> dict:
    """Index a project's codebase and start watching it for changes."""

@mcp.tool()
async def search_code(query: str, path: str = ".", limit: int = 10,
                      federated_paths: list[str] | None = None) -> dict:
    """Semantically search indexed code. Works across multiple projects."""

@mcp.tool()
async def project_status(path: str = ".") -> dict:
    """Return indexing status, watcher state, and chunk count for a project."""

@mcp.tool()
async def list_indexed_projects() -> list[dict]:
    """List all projects in the registry with their index metadata."""

@mcp.tool()
async def stop_watching(path: str = ".") -> dict:
    """Stop the file watcher for a project."""
```

### 8c — MCP prompt (`/index` slash command)
```python
@mcp.prompt()
def index_current_project() -> str:
    """Slash command: /index — indexes the current project and starts watching."""
    return "Please index the current project using the index_project tool with path='.', then confirm the project is being watched."
```

### 8d — Auto-watch on session start
```python
async def _auto_watch_on_init(roots: list[str]) -> None:
    registry = config.load_project_registry()
    for root in roots:
        if root in registry and not watcher_manager.is_active(root):
            await watcher_manager.start(root, registry[root])
```
Called from MCP `initialize` handler — zero user action on subsequent sessions.

### 8e — Project registry (`~/.opencode/projects.json`)
```json
{
  "/path/to/project": {
    "db_path": "/path/to/.lancedb",
    "tier": "premium",
    "dims": 768,
    "indexed_at": "2026-05-22T10:00:00Z",
    "file_count": 6014,
    "last_active": "2026-05-22T12:00:00Z"
  }
}
```
Updated atomically (write `.tmp`, rename) after every `index_project` call.

### 8f — JSON-RPC backward compatibility
Keep `POST /rpc` + `GET /ping` on port 9393 via FastAPI running as background task. Existing TypeScript orchestrators continue to work.

**RPC methods to port from `handlers.rs`:**
- `ping`, `resolve_paths`
- `index_file`, `index_files`, `remove_file`
- `search`, `search_memories`, `search_activity`, `search_skills`
- `status`, `watcher_start`, `watcher_stop`, `watcher_status`
- `compaction_force`
- `tui_connect`, `tui_disconnect`, `tui_connections`

**Risk:** High — largest phase. Build MCP tools one at a time, test with `mcp dev`.

---

## Phase 9 — CLI

**Goal:** Port `cli.rs` CLI flags → `cli.py` with `typer`.

**Steps:**
1. `app = typer.Typer()` with commands: default (index), `daemon`, `search`, `status`, `mcp`
2. Flags: `--root`, `--db`, `--tier`, `--search`, `--force`, `--federated-db`, `--health`, `--port`, `--parent-pid`, `--dimensions`, `--mcp`, `--daemon`
3. `--mcp` flag: start the MCP server (stdio)
4. `--daemon` flag: start HTTP JSON-RPC server on `--port` (backward compat)
5. JSON-lines progress to stdout (TypeScript consumer compatibility)
6. Version string from `importlib.metadata`

**Risk:** Low.

---

## Phase 10 — Process Management + Health Supervisor

**Goal:** Port `process_group.rs` → Python; update `health-supervisor.sh` for single process.

**Steps:**
1. `hardware.py` — GPU via `nvidia-smi` (reuse `_detect_gpu_capabilities()`), CPU via `os.cpu_count()`, RAM via `psutil`
2. `os.setpgrp()` on startup (already in current `server.py`)
3. OOM score: write `-500` to `/proc/self/oom_score_adj`
4. Parent PID monitor: `asyncio` task polling `os.kill(parent_pid, 0)` every 5s
5. Update `health-supervisor.sh` — monitors one process (`python -m opencode_search --mcp`) instead of two

**Risk:** Low.

---

## Phase 11 — Global MCP Config (Claude Code, Codex, Hermes)

**Goal:** Install MCP server globally so it auto-loads in every project for every AI assistant.

### Claude Code
```bash
uv tool install --editable /path/to/opencode-search-engine/src
claude mcp add --global opencode-search \
  python -m opencode_search \
  --env OPENCODE_GPU_ONLY=1 \
  --env OPENCODE_GPU_NORMALIZE=gpu
```

`~/.claude/claude_mcp_config.json`:
```json
{
  "mcpServers": {
    "opencode-search": {
      "command": "python",
      "args": ["-m", "opencode_search"],
      "env": {
        "OPENCODE_GPU_ONLY": "1",
        "OPENCODE_GPU_NORMALIZE": "gpu"
      }
    }
  }
}
```

Slash command: `/mcp__opencode-search__index_current_project`

### Codex
`~/.codex/config.toml`:
```toml
[[mcp_servers]]
name = "opencode-search"
command = ["python", "-m", "opencode_search"]
env = { OPENCODE_GPU_ONLY = "1" }
```

### Hermes
Similar JSON config — same `command`/`env` pattern.

### Auto-watch behavior (all assistants)
1. MCP server spawns on assistant start
2. Reads `MCP_ROOTS` or CWD from MCP `initialize`
3. If in `~/.opencode/projects.json` → watcher auto-starts
4. If not → `index_project` tool available

**Risk:** Low for Claude Code. Medium for Codex/Hermes.

---

## Phase 12 — E2E Tests

See `E2E_TESTING.md` for the full detailed test plan.

---

## Phase Summary

| Phase | What | Risk | Depends on |
|---|---|---|---|
| 1 | Restructure + scaffolding | Low | — |
| 2 | Storage layer | Medium | 1 |
| 3 | File discovery | Low | 1 |
| 4 | GPU-maximized embeddings | Medium | 1 |
| 5 | Indexing pipeline | Medium | 2, 3, 4 |
| 6 | File watcher | Medium | 3, 5 |
| 7 | Search | Low-medium | 2, 4 |
| 8 | MCP server (daemon) | High | 5, 6, 7 |
| 9 | CLI | Low | 5, 7, 8 |
| 10 | Process management | Low | 8 |
| 11 | Global MCP configs | Low | 8, 9 |
| 12 | E2E tests | Low | all |

**Total estimated output:** ~10–12K lines of Python replacing 17K lines of Rust + 4.7K lines of Python (embedder). One language, one process, direct GPU calls, no IPC.
