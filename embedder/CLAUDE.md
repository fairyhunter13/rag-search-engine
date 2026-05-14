# CRITICAL: GPU Enforcement Rule

**ALL operations that CAN run on GPU MUST be offloaded to GPU.** This is non-negotiable.

- FORBIDDEN to use CPU for GPU-capable workloads (embedding, reranking, inference)
- FORBIDDEN to hog CPU and memory — causes device crash, kernel panic, and lagging
- ONNX Runtime MUST use GPU providers: CUDA > TensorRT > CPU (absolute last resort)
- Minimize CPU and memory footprint for all Python services
- CPU is acceptable ONLY for inherently CPU-bound tasks:
  - tree-sitter AST parsing (single-threaded C library, no GPU path)
  - Text splitting (pure Python text processing)
  - Tokenizer counting (HuggingFace tokenizers, Rust CPU)
  - File I/O and magika detection (I/O bound)
  - SemanticChunker potion-base-32M (static lookup, sub-ms, faster than GPU launch)

Violating this rule risks device crash and kernel panic.

# opencode-embedder

## Requirements

- Python 3.12+, aiohttp, ONNX Runtime GPU
- GPU provider priority: CUDA > TensorRT > CPU (last resort)
- Install with `make sync-gpu` to ensure `onnxruntime-gpu` overwrites CPU `onnxruntime`
- Auto-detects GPU at startup (`nvidia-smi`, `rocm-smi`, or Apple Silicon CoreML)

## Architecture

```
Rust indexer ──► HTTP (127.0.0.1:9998) ──► aiohttp handlers ──►─┐
                                                                    │
                                          ┌─ EmbedBatcher ─────────┤
                                          │  (coalesce → GPU batch) │
                                          └─ N embed workers ───────┘
                                                   │
                                              ONNX Runtime (CUDA/TensorRT)
                                                   │
                                                  GPU
```

- aiohttp HTTP server, single process, N embed workers
- Embed workers gated by `asyncio.Semaphore(OPENCODE_EMBED_WORKERS)`
- Chunking gated by separate `_chunk_sem` to prevent CPU oversubscription
- `EmbedBatcher` coalesces small requests into GPU batches (max 64 texts)
- ThreadPoolExecutor sized as `cpu_count / OMP_NUM_THREADS`

## GPU vs CPU Responsibilities

### GPU-accelerated (ONNX Runtime via FastEmbed)

| Operation      | Model                           | Provider              |
| -------------- | ------------------------------- | --------------------- |
| Text embedding | `jinaai/jina-embeddings-v2-*`   | CUDA > TensorRT       |
| Reranking      | `Xenova/ms-marco-MiniLM-L-6-v2` | CUDA > TensorRT       |
| Query embedding| Same as text embedding          | Same provider chain   |

### CPU-bound (intentional, acceptable)

| Operation                                   | Reason                                                                           |
| ------------------------------------------- | -------------------------------------------------------------------------------- |
| `SemanticChunker` (potion-base-32M)         | Static lookup + linear projection, sub-ms, faster than GPU launch overhead       |
| Tree-sitter AST parsing (`CodeChunker`)     | Single-threaded C library, no GPU acceleration available                         |
| LangChain splitters (Markdown, JSON, HTML)   | Pure Python text processing                                                      |
| Tokenizer counting                          | HuggingFace tokenizers (Rust, CPU)                                               |
| File I/O + magika detection                 | I/O bound, not compute                                                           |

## HTTP Endpoints

| Endpoint                         | Purpose                                    |
| -------------------------------- | ------------------------------------------ |
| `POST /embed/chunk`              | Chunk text (tree-sitter + tokenizer)       |
| `POST /embed/chunk_file`         | Chunk a file from path                     |
| `POST /embed/chunk_and_embed`    | Chunk + embed in one request (most common) |
| `POST /embed/chunk_and_embed_f32`| Chunk + embed, returns float32 bytes       |
| `POST /embed/passages`           | Embed pre-chunked text passages            |
| `POST /embed/passages_f32`       | Embed passages, returns float32 bytes      |
| `POST /embed/query`              | Embed a search query                       |
| `POST /embed/query_f32`          | Embed query, returns float32 bytes         |
| `POST /embed/rerank`             | Rerank search results                      |
| `GET /health`                    | Health check + GPU stats                   |
| `POST /shutdown`                 | Graceful shutdown                          |

## Environment Variables

| Variable                      | Purpose                                          | Default        |
| ----------------------------- | ------------------------------------------------ | -------------- |
| `OPENCODE_EMBED_HTTP_PORT`    | HTTP server port                                 | `9998`         |
| `OPENCODE_EMBED_WORKERS`      | Number of embed workers (`auto` = detect)        | auto-detected  |
| `OPENCODE_EMBED_LOW_MEMORY`   | Cap workers at 2 for low-memory devices          | unset          |
| `OPENCODE_EMBEDDER_FORCE_GPU` | Enforce GPU (exit if unavailable)                | `1` on NVIDIA  |
| `OPENCODE_ONNX_PROVIDER`      | Force specific ONNX provider (`cuda`/`cpu`/etc.) | auto-detected  |
| `OPENCODE_ONNX_PROVIDERS`     | Explicit provider list (comma-separated)         | auto-detected  |
| `OPENCODE_GPU_REQUIRED`       | Exit if GPU is not available                     | `1` on NVIDIA  |
| `ORT_NUM_THREADS`             | ONNX Runtime intra-op threads                    | `2`            |
| `OMP_NUM_THREADS`             | OpenMP threads for ONNX                          | auto           |
| `OPENCODE_COALESCE_BATCH`     | Max texts per GPU batch                          | `64`           |
| `OPENCODE_EMBED_SUB_BATCH`    | Texts per ONNX forward pass                      | 64–128 (auto)  |
| `OPENCODE_EMBED_IDLE_SHUTDOWN`| Auto-shutdown after idle seconds                 | unset          |

## GPU Enforcement Validation

At startup, the server logs:

```
[embedder] GPU ACTIVE: Using CUDAExecutionProvider for inference
[reranker] GPU ACTIVE: Using CUDAExecutionProvider for inference
```

Verify GPU is active via health endpoint:

```bash
curl http://localhost:9998/health | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('gpu_stats',{}))"
# Expected: {"is_gpu": true, "provider": "cuda", ...}
```

If `GPU DEGRADED` appears in logs, the ONNX session fell back to CPU.

## Chunking Pipeline

1. **magika** — detect file type (code, text, markdown, JSON, etc.)
2. **Tree-sitter** — AST-aware chunking for code files (respects function/class boundaries)
3. **LangChain splitters** — fallback for prose files (Markdown, JSON, HTML)
4. **SemanticChunker** — last resort for unknown text (potion-base-32M)
5. **Tokenizer** — count tokens per chunk, enforce max_token limit

Each file generates ~6 chunks on average for TypeScript repos.

## Performance

Typical throughput: ~37 embeddings/second on RTX 5080 (16GB VRAM).
The bottleneck is **chunking** (tree-sitter on CPU), not GPU inference.
GPU utilization: ~9% during indexing (embedding is fast, chunking is slow).
