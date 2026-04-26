# CRITICAL: GPU Enforcement Rule

**ALL operations that CAN run on GPU MUST be offloaded to GPU.** This is non-negotiable.

- FORBIDDEN to use CPU for GPU-capable workloads (embedding, reranking, inference)
- FORBIDDEN to hog CPU and memory — causes device crash, kernel panic, and lagging
- ONNX Runtime MUST use GPU providers: CUDA > TensorRT > other GPU > CPU (absolute last resort)
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

- The Python model server **must** use GPU (CUDA) for inference. Never fall back to CPU-only mode in production.
- GPU provider priority: TensorRT > CUDA > CPU fallback (CPU is only acceptable as a last-resort fallback within ONNX provider chains, not as the primary execution mode).
- Install with `make sync-gpu` (not `make sync`) to ensure `onnxruntime-gpu` overwrites the CPU `onnxruntime` that `magika` pulls in.

## Architecture

- Python HTTP server (aiohttp) with N embed workers gated by `asyncio.Semaphore`
- Chunking (tree-sitter + tokenizers) is also gated by `_chunk_sem` to prevent CPU oversubscription
- ThreadPoolExecutor is sized as `cpu_count / OMP_NUM_THREADS` to keep total threads <= CPU count
- ONNX Runtime runs inference; `OMP_NUM_THREADS` controls internal parallelism per session

## GPU vs CPU Responsibilities

### GPU-accelerated (ONNX Runtime via FastEmbed)

| Operation      | Model                           | Provider                                     |
| -------------- | ------------------------------- | -------------------------------------------- |
| Text embedding | `jinaai/jina-embeddings-v2-*`   | TensorRT > CUDA > MIGraphX > ROCm > DirectML |
| Reranking      | `Xenova/ms-marco-MiniLM-L-6-v2` | Same provider chain                          |

### CPU-bound (intentional, acceptable)

| Operation                                   | Reason                                                                                                                                          |
| ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `SemanticChunker` (potion-base-32M)         | `model2vec.StaticModel` is CPU-only by design — static lookup + linear projection via numpy, sub-millisecond, no GPU path exists in the library |
| Tree-sitter AST parsing (`CodeChunker`)     | Single-threaded C library; no GPU acceleration available                                                                                        |
| LangChain splitters (Markdown, JSON, HTML…) | Pure Python text processing                                                                                                                     |
| Tokenizer counting                          | HuggingFace tokenizers (Rust, CPU)                                                                                                              |
| File I/O + magika detection                 | I/O bound, not compute                                                                                                                          |

### Why SemanticChunker on CPU is acceptable

- `potion-base-32M` uses a static embedding model: word vector lookup + linear transform
- No attention layers → no O(n²) memory explosion, no large compute graphs
- Inference is ~0.1ms per sentence on CPU (faster than GPU launch overhead)
- Only used for plain-text/prose files (`.txt`, unknown extensions) — rare in code repos
- The prose chunks it produces are then embedded by FastEmbed **on GPU**

## GPU Enforcement Validation

At startup, the server logs:

```
[embedder] GPU ACTIVE: Using CUDAExecutionProvider for inference
[reranker] GPU ACTIVE: Using CUDAExecutionProvider for inference
```

If you see `GPU DEGRADED` in logs, a GPU provider was requested but the ONNX session
fell back to CPU (driver mismatch, missing shared libraries, etc.). This is a failure state.

To verify GPU is active:

```bash
# Check server health endpoint
curl http://localhost:5001/health | jq .gpu_stats

# Expected output includes:
# "is_gpu": true
# "provider": "cuda" or "tensorrt" or "migraphx"
# "degraded" key should be absent
```

Environment overrides:

- `OPENCODE_ONNX_PROVIDER=cuda` — force CUDA (skip TensorRT)
- `OPENCODE_ONNX_PROVIDER=cpu` — force CPU (testing only)
- `OPENCODE_ONNX_PROVIDERS=CUDAExecutionProvider,CPUExecutionProvider` — explicit list
