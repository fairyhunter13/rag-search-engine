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
