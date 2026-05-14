# opencode-indexer

Rust daemon that watches the filesystem and coordinates semantic indexing via the Python embedder.

## Architecture

```
OpenCode TUI/Server (Bun)
    │  abstract Unix socket: @opencode-indexer
    ▼
opencode-indexer (Rust daemon, this binary)
    │  HTTP POST to embedder on 127.0.0.1:9998
    ▼
opencode-embedder (Python aiohttp, GPU inference)
    │  ONNX Runtime via CUDA/TensorRT
    ▼
GPU (embedding + reranking)
```

The indexer runs as a persistent daemon (`--daemon`), listening on the abstract
Unix socket `\0opencode-indexer`. The OpenCode server (Bun, `opencode serve`)
connects to this socket for all index operations (search, status, watcher, etc.).

The embedder is spawned as a child process (`--parent-pid`) with GPU enforcement
(`OPENCODE_EMBEDDER_FORCE_GPU=1`). All communication is HTTP on localhost.

## Event-Driven Watching (NOT Polling)

| Platform | Mechanism               | Notes                                    |
| -------- | ----------------------- | ---------------------------------------- |
| Linux    | `inotify`               | Kernel-level file event notifications    |
| macOS    | `FSEvents`              | Core Services event stream               |
| Windows  | `ReadDirectoryChangesW` | Win32 API directory change notifications |

Implemented via the `notify` crate (`src/watcher.rs`). **~0% CPU when idle.**

### inotify Watch Limits (Linux)

The indexer manually walks directories and adds `NonRecursive` watches per directory
to avoid exhausting `/proc/sys/fs/inotify/max_user_watches`.

```bash
cat /proc/sys/fs/inotify/max_user_watches
sudo sysctl fs.inotify.max_user_watches=524288
```

## Data Flow (per file)

```
Filesystem event (inotify/FSEvents)
    ↓
watcher.rs    →  debounce (500ms)
    ↓
worker.rs     →  content hash check (blake3), skip unchanged
    ↓
model_client.rs →  HTTP POST /embed/chunk_and_embed  →  Python embedder
    ↓                                                    ├─ Chunk: tree-sitter AST (CPU)
    │                                                    └─ Embed: ONNX Runtime (GPU)
    ↓
storage.rs    →  LanceDB upsert (vectors + metadata, atomic per-file)
```

## CPU vs GPU Responsibilities

### Indexer (this binary) — CPU / I/O bound

All work is I/O-bound or lightweight CPU, capped at 4 threads:

| Operation                           | Notes                                     |
| ----------------------------------- | ----------------------------------------- |
| Filesystem watching                 | Kernel-level inotify — ~0 CPU when idle   |
| File discovery / directory walking  | I/O bound                                 |
| Content deduplication (blake3)      | CPU, fast                                 |
| LanceDB vector storage              | CPU + disk I/O                            |
| HTTP client to embedder             | Network I/O, localhost                    |
| Debouncing + rate limiting          | Timer-based, negligible CPU               |

Thread pools are capped via `TOKIO_WORKER_THREADS=4`, `RAYON_NUM_THREADS=4`.

### Embedder (Python sidecar) — GPU inference

| Operation                              | Hardware                                     |
| -------------------------------------- | -------------------------------------------- |
| Text embedding (`jina-embeddings-v2-*`) | GPU (ONNX Runtime: CUDA > TensorRT)          |
| Reranking (`ms-marco-MiniLM`)           | GPU (same ONNX provider chain)               |
| Semantic chunking (`potion-base-32M`)   | CPU (static lookup, sub-ms, faster than GPU) |
| Tree-sitter AST chunking               | CPU (C library, no GPU path)                 |
| Tokenizer counting                     | CPU (Rust, HuggingFace tokenizers)           |

The embedder uses batched GPU inference via `EmbedBatcher`:
- Coalesces small requests into GPU batches (max 64 texts)
- Sub-batch size: 64–128 texts per ONNX forward pass
- Workers: auto-detected based on GPU VRAM, capped by `OPENCODE_EMBED_WORKERS`

## Embedder HTTP Endpoints (localhost:9998)

| Endpoint                      | Purpose                                    |
| ----------------------------- | ------------------------------------------ |
| `POST /embed/chunk`           | Chunk text (tree-sitter + tokenizer)       |
| `POST /embed/chunk_file`      | Chunk a file from path                     |
| `POST /embed/chunk_and_embed` | Chunk + embed in one request (most common) |
| `POST /embed/passages`        | Embed pre-chunked text passages            |
| `POST /embed/query`           | Embed a search query                       |
| `POST /embed/rerank`          | Rerank search results                      |
| `GET /health`                 | Health check + GPU stats                   |

## Indexer Unix Socket RPC (abstract: @opencode-indexer)

The OpenCode server sends JSON-RPC via `POST /rpc`:

| Method             | Purpose                         |
| ------------------ | ------------------------------- |
| `search`           | Semantic code search            |
| `search_memories`  | Cross-project memory search     |
| `search_activity`  | Session activity search         |
| `search_skills`    | Skill discovery search          |
| `status`           | Index status (files, progress)  |
| `run_index`        | Start full project indexing     |
| `watcher_start`    | Start file watcher              |
| `watcher_stop`     | Stop file watcher               |
| `watcher_status`   | Watcher health + TUI connections|
| `startup_check`    | Auto-fix corruption, start watcher |
| `tui_connect`      | Register TUI connection         |
| `tui_disconnect`   | Unregister TUI connection       |
| `health`           | Daemon health check             |
| `ping`             | Liveness probe (returns "pong") |

## Storage (LanceDB)

- Vectors stored per-chunk with `chunk_id = "{path}#{chunk_index}"`
- Upsert logic: delete all chunks for a path, then insert new chunks atomically
- Prevents chunk_id collisions on restart
- Index compaction runs after batch operations

## Performance

Typical indexing rate: ~5–7 files/second for a TypeScript monorepo.
The primary bottleneck is **chunking** (tree-sitter AST parsing on CPU),
not GPU embedding. Each file generates ~6 chunks on average.

GPU: RTX 5080 Laptop (16GB VRAM) — ~9% utilization during indexing.
CPU: ~30% utilization (chunking + I/O).
Memory: ~1.9% (embedder process) + ~0.5% (indexer process).
