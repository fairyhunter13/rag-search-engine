"""Global Python model server — HTTP-only, singleton shared by all Rust indexer instances.

Architecture (HTTP + N embed workers):

    N Rust indexers ──► HTTP (127.0.0.1) ──► aiohttp handlers ──►─┐
                                                                   │
                                               Direct dispatch     │
                                               to handler          │
                                                                   ▼
                                          ┌─────────────────────────────┐
                                          │      Embed Worker Pool      │
                                          │  (N workers, semaphore)     │
                                          └─────────────────────────────┘

Embed workers:
    - Controlled by asyncio.Semaphore(N)
    - Each runs ONNX inference in a thread (ONNX releases the GIL)
    - N=2-3 for GPU mode, N=2-4 for CPU mode (auto-detected)

On-demand spawning support:
    The server supports auto-shutdown after idle time, enabling on-demand spawning
    via SSH from remote clients. Set OPENCODE_EMBED_IDLE_SHUTDOWN=600 (10 min default)
    or 0 to disable. When idle for the timeout period, the server shuts down gracefully.

Environment variables:
    OPENCODE_EMBED_HTTP_PORT:     HTTP port (default: 9998)
    OPENCODE_EMBED_WORKERS:       Number of embed workers (auto-detected by default)
    OPENCODE_EMBED_IDLE_SHUTDOWN: Idle timeout before auto-shutdown (default: 600s, 0=disable)
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import concurrent.futures
import fcntl
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from aiohttp import web

# Debug file logging (since stdout/stderr go to /dev/null when spawned)
_DEBUG_LOG_PATH = os.environ.get("OPENCODE_EMBED_DEBUG", "/tmp/embedder-debug.log")
_DEBUG_LOG = None


def _debug_log(msg: str):
    """Write debug message to file (bypasses null stdout/stderr)."""
    global _DEBUG_LOG
    if _DEBUG_LOG is None:
        try:
            _DEBUG_LOG = open(_DEBUG_LOG_PATH, "a", buffering=1)  # Line buffered
        except:
            return
    try:
        import time as time_mod

        _DEBUG_LOG.write(f"[{time_mod.strftime('%H:%M:%S')}] {msg}\n")
        _DEBUG_LOG.flush()
    except:
        pass


# ---------------------------------------------------------------------------
# Process group cleanup utilities
# ---------------------------------------------------------------------------


def _setup_process_group() -> None:
    """Set up process group for clean child process termination.

    On Linux, uses prctl(PR_SET_PDEATHSIG) to ensure child processes receive
    SIGTERM when the parent dies (even from SIGKILL). On other platforms,
    we rely on process group signaling at exit.
    """
    try:
        # Try to become session leader (new process group)
        # This ensures all spawned children are in our group
        os.setpgrp()
    except OSError:
        pass  # Already a session leader or not allowed

    # On Linux, set parent death signal for any children we spawn
    if sys.platform == "linux":
        try:
            import ctypes

            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            PR_SET_PDEATHSIG = 1
            SIGTERM = 15
            # This affects future children, not existing ones
            libc.prctl(PR_SET_PDEATHSIG, SIGTERM)
        except Exception:
            pass  # Best-effort


def _kill_process_group() -> None:
    """Kill all processes in our process group.

    Called on exit to ensure no orphaned child processes (e.g., ONNX
    inference threads that might be holding GPU resources).
    """
    try:
        pgid = os.getpgid(0)
        # Send SIGTERM to entire process group (negative PID)
        os.killpg(pgid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass  # Process group may already be gone


# Set up cleanup on normal exit
atexit.register(_kill_process_group)

from opencode_embedder import chunker, tokenizer as tok
from opencode_embedder.embeddings import (
    cleanup_models,
    embed_passages,
    embed_passages_f32_bytes,
    embed_query,
    embed_query_f32_bytes,
    get_active_provider,
    get_gpu_stats,
    is_gpu_available,
    rerank,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
OPENCODE_DIR = Path.home() / ".opencode"


# ---------------------------------------------------------------------------
# Hardware detection
# ---------------------------------------------------------------------------
def _detect_hardware() -> tuple[int, int]:
    """Detect logical CPU count and total RAM in MB."""
    cpus = os.cpu_count() or 2
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    ram_mb = int(line.split()[1]) // 1024
                    return cpus, ram_mb
    except (OSError, ValueError):
        pass
    # macOS / fallback
    try:
        import subprocess

        out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)
        return cpus, int(out.strip()) // (1024 * 1024)
    except Exception:
        return cpus, 8192  # assume 8 GB if unknown


_CPUS, _RAM_MB = _detect_hardware()
_LOW_END = _CPUS <= 4 or _RAM_MB <= 8192  # ≤4 threads or ≤8 GB RAM


# Performance tiers:
#   LOW_END:  ≤4 CPUs or ≤8 GB RAM
#   HIGH_END: ≥16 CPUs and ≥32 GB RAM
_HIGH_END = _CPUS >= 16 and _RAM_MB >= 32768

# Worker scaling based on execution mode (CPU vs GPU).
#
# CPU mode: Limited by CPU cores. Each worker holds an ONNX session (~150-250 MB).
#   - Sweet spot is ~1 worker per 4 CPUs to avoid contention.
#   - Memory estimate: 2 workers = ~400 MB, 4 workers = ~800 MB
#
# GPU mode: GPU can handle high parallelism. Workers just queue requests.
#   - GPU inference is much faster than CPU, so we need more workers to saturate it.
#   - Each worker just dispatches to GPU, minimal CPU overhead.
#   - 24-48 workers is typical for GPU servers.


def _detect_embed_workers() -> int:
    """Auto-detect optimal number of embed workers based on hardware.

    For GPU mode, the key constraint is GPU memory contention, NOT CPU cores.
    Each concurrent ONNX session allocates GPU memory for intermediate tensors.
    Too many concurrent sessions cause GPU OOM or severe contention (3-5x slowdown).

    Profiling on RX 7900 XTX (24GB VRAM) with 48-text batches:
      1 worker:  3.2 f/s (GPU underutilized)
      2 workers: 9.0 f/s (GPU ~100% utilized, optimal)
      3 workers: 9.0 f/s (same — GPU already saturated)
      4 workers: 8.9 f/s (slight contention)
      16 workers: 7.4 f/s (heavy contention, 3-5s per batch vs 1s)

    The network concurrency (16 TCP connections) is handled by asyncio tasks
    that wait on the embed semaphore. Only `workers` ONNX sessions run at once.

    Can be overridden via OPENCODE_EMBED_WORKERS environment variable.
    """
    # Check for manual override first
    override = os.environ.get("OPENCODE_EMBED_WORKERS", "").strip()
    if override:
        try:
            workers = int(override)
            if workers > 0:
                return workers
        except ValueError:
            pass

    # Auto-detect based on GPU availability
    if is_gpu_available():
        # GPU mode: scale by VRAM, not CPU cores.
        # Each concurrent ONNX session needs ~1-2GB VRAM for attention buffers.
        # Sweet spot is 2-3 for consumer GPUs (8-24GB), 3-4 for data center (48-80GB).
        from opencode_embedder.embeddings import _get_gpu_vram_mb

        vram_mb = _get_gpu_vram_mb()
        if vram_mb is not None:
            vram_gb = vram_mb / 1024
            if vram_gb < 6:
                return 1
            if vram_gb < 12:
                return 2
            if vram_gb < 32:
                return 3  # 12-32GB (RTX 3090, RX 7900 XTX, A5000)
            if vram_gb < 64:
                return 4  # 32-64GB (A100 40GB, A6000)
            return 6  # 64GB+ (A100 80GB, H100)

        # Fallback: no VRAM detection, conservative
        return 2

    # CPU mode: limited by CPU cores
    if _LOW_END:
        return 2
    elif _HIGH_END:
        return min(6, max(4, _CPUS // 4))
    else:
        return min(4, max(2, _CPUS // 4))


EMBED_WORKERS = _detect_embed_workers()

# Sub-batch size: match embeddings.py for consistency
if _LOW_END:
    EMBED_SUB_BATCH = 64
elif _HIGH_END:
    EMBED_SUB_BATCH = 128
else:
    EMBED_SUB_BATCH = 96

# Idle cleanup: release memory when idle
IDLE_CLEANUP_SECS = 120 if _RAM_MB <= 16384 else 300


# Idle shutdown: auto-shutdown server after no activity (for on-demand spawning)
# Default: 10 minutes. Set OPENCODE_EMBED_IDLE_SHUTDOWN=0 to disable.
# This is useful when the embedder is spawned on-demand via SSH from a remote client.
def _get_idle_shutdown_secs() -> int:
    """Get idle shutdown timeout from environment, or default to 600 (10 min)."""
    val = os.environ.get("OPENCODE_EMBED_IDLE_SHUTDOWN", "").strip()
    if not val:
        return 600  # Default: 10 minutes
    try:
        return int(val)
    except ValueError:
        return 600


IDLE_SHUTDOWN_SECS = _get_idle_shutdown_secs()


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
class ModelServer:
    """HTTP model server with parallel embed workers."""

    def __init__(
        self,
        embed_workers: int = EMBED_WORKERS,
        idle_shutdown_secs: int = IDLE_SHUTDOWN_SECS,
    ) -> None:
        self._shutdown = asyncio.Event()
        self._last_activity = time.monotonic()
        self._embed_sem = asyncio.Semaphore(embed_workers)
        # Limit concurrent chunking to avoid CPU oversubscription.
        # Chunking is CPU-intensive (tree-sitter + tokenizers) and each thread
        # spawns OMP_NUM_THREADS internal threads, so unbounded concurrency
        # causes total_threads = pool_size × OMP_threads >> cpu_count.
        self._chunk_sem = asyncio.Semaphore(max(2, embed_workers))
        self._embed_workers = embed_workers
        self._idle_shutdown_secs = idle_shutdown_secs

    # ---- Idle shutdown monitor ----

    async def _idle_shutdown_monitor(self) -> None:
        """Monitor for idle time and trigger shutdown when no activity."""
        if self._idle_shutdown_secs <= 0:
            log.info("idle shutdown disabled (OPENCODE_EMBED_IDLE_SHUTDOWN=0)")
            return

        log.info("idle shutdown monitor started (timeout=%ds)", self._idle_shutdown_secs)

        while not self._shutdown.is_set():
            idle = time.monotonic() - self._last_activity
            remaining = max(1.0, self._idle_shutdown_secs - idle)

            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=remaining)
                break
            except asyncio.TimeoutError:
                pass

            idle = time.monotonic() - self._last_activity
            if idle >= self._idle_shutdown_secs:
                log.info("idle shutdown: no activity for %.0fs", idle)
                self._shutdown.set()
                break

        log.info("idle shutdown monitor stopped")

    # ---- Method handlers ----

    async def _handle_chunk(self, params: dict) -> dict:
        """Chunk a file without embedding."""
        # Offload blocking _chunk call to thread pool (gated by chunk semaphore)
        async with self._chunk_sem:
            return {"chunks": await asyncio.to_thread(self._chunk, params)}

    def _chunk(self, params: dict) -> list[dict]:
        """Chunk content without embedding. Used by batch coalescing."""
        file = params.get("file")
        if file:
            try:
                data = Path(file).read_bytes()
                if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
                    content = data.decode("utf-16", errors="ignore")
                else:
                    # utf-8-sig strips BOM if present
                    content = data.decode("utf-8-sig", errors="ignore")
            except Exception:
                content = ""
        else:
            content = params.get("content", "")
        path = Path(params.get("path", "file.txt"))
        tier = params.get("tier", "budget")

        chunker.set_tier(tier)
        tok.ensure_tokenizer_for_tier(tier)
        chunks = chunker.chunk_file(content, path)

        return [
            {
                "content": c.content,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "chunk_type": c.chunk_type,
                "language": c.language,
            }
            for c in chunks
        ]

    async def _handle_chunk_and_embed(self, params: dict) -> dict:
        """Chunk a file and embed all chunks (single request path)."""
        # Offload blocking _chunk call to thread pool (gated by chunk semaphore)
        async with self._chunk_sem:
            chunks = await asyncio.to_thread(self._chunk, params)
        if not chunks:
            return {"chunks": []}

        model = params.get("model", "")
        dimensions = params.get("dimensions", 1024)
        texts = [c["content"] for c in chunks]

        # Use embed worker semaphore even for single requests
        async with self._embed_sem:
            vectors = await asyncio.to_thread(
                embed_passages, texts, model=model, dimensions=dimensions
            )

        for chunk, vec in zip(chunks, vectors):
            chunk["vector"] = vec

        return {"chunks": chunks}

    async def _handle_chunk_and_embed_f32(self, params: dict) -> dict:
        """Chunk a file and return f32 bytes for vectors."""
        # Offload blocking _chunk call to thread pool (gated by chunk semaphore)
        async with self._chunk_sem:
            chunks = await asyncio.to_thread(self._chunk, params)
        if not chunks:
            return {
                "chunks": [],
                "vectors_f32": b"",
                "dimensions": int(params.get("dimensions", 1024)),
                "count": 0,
                "endianness": "le",
            }

        model = params.get("model", "")
        dimensions = int(params.get("dimensions", 1024))
        texts = [c["content"] for c in chunks]

        async with self._embed_sem:
            buf, dims, count = await asyncio.to_thread(
                embed_passages_f32_bytes, texts, model=model, dimensions=dimensions
            )

        return {
            "chunks": chunks,
            "vectors_f32": buf,
            "dimensions": dims or dimensions,
            "count": count,
            "endianness": "le",
        }

    async def _handle_embed_query(self, params: dict) -> dict:
        """Embed a search query (HIGH priority, bypasses coalescing)."""
        text = params.get("text", "")
        model = params.get("model", "")
        dimensions = params.get("dimensions", 1024)

        async with self._embed_sem:
            vector = await asyncio.to_thread(embed_query, text, model=model, dimensions=dimensions)
        return {"vector": vector}

    async def _handle_embed_query_f32(self, params: dict) -> dict:
        """Embed a search query and return f32 bytes (HIGH priority)."""
        # Accept both "query" (from Rust client) and "text" for backwards compatibility
        text = params.get("query", params.get("text", ""))
        model = params.get("model", "")
        dimensions = int(params.get("dimensions", 1024))

        async with self._embed_sem:
            buf, dims = await asyncio.to_thread(
                embed_query_f32_bytes, text, model=model, dimensions=dimensions
            )

        return {
            "vector_f32": buf,
            "dimensions": dims or dimensions,
            "endianness": "le",
        }

    async def _handle_embed_passages(self, params: dict) -> dict:
        """Embed multiple passages."""
        texts = params.get("texts", [])
        model = params.get("model", "")
        dimensions = params.get("dimensions", 1024)

        t0 = time.monotonic()
        async with self._embed_sem:
            t_sem = time.monotonic()
            vectors = await asyncio.to_thread(
                embed_passages, texts, model=model, dimensions=dimensions
            )
        t_done = time.monotonic()
        log.debug(
            "embed_passages: %d texts, sem_wait=%.1fms, inference=%.1fms",
            len(texts),
            (t_sem - t0) * 1000,
            (t_done - t_sem) * 1000,
        )
        return {"vectors": vectors}

    async def _handle_embed_passages_f32(self, params: dict) -> dict:
        """Embed multiple passages and return f32 bytes."""
        # Accept both "passages" (from Rust client) and "texts" for backwards compatibility
        texts = params.get("passages", params.get("texts", []))
        model = params.get("model", "")
        dimensions = int(params.get("dimensions", 1024))

        t0 = time.monotonic()
        async with self._embed_sem:
            t_sem = time.monotonic()
            buf, dims, count = await asyncio.to_thread(
                embed_passages_f32_bytes, texts, model=model, dimensions=dimensions
            )
        t_done = time.monotonic()
        log.debug(
            "embed_passages_f32: %d texts, sem_wait=%.1fms, inference=%.1fms",
            len(texts),
            (t_sem - t0) * 1000,
            (t_done - t_sem) * 1000,
        )
        return {
            "vectors_f32": buf,
            "dimensions": dims or dimensions,
            "count": count,
            "endianness": "le",
        }

    async def _handle_rerank(self, params: dict) -> dict:
        """Rerank documents against a query (HIGH priority)."""
        query = params.get("query", "")
        # Accept both "docs" and "passages" (Rust sends "passages")
        docs = params.get("docs") or params.get("passages", [])
        model = params.get("model", "")
        top_k = params.get("top_k", 10)

        async with self._embed_sem:
            ranked = await asyncio.to_thread(rerank, query, docs, model=model, top_k=top_k)
        # Note: Rust expects "results" key, not "ranked"
        return {"results": [{"index": idx, "score": score} for idx, score in ranked]}

    def _handle_health(self) -> dict:
        """Health check — returns server status with GPU info.

        Reports 'degraded' status when GPU was expected but ONNX fell back to CPU.
        """
        gpu_stats = get_gpu_stats()
        is_degraded = gpu_stats.get("degraded", False)
        result = {
            "status": "degraded" if is_degraded else "ok",
            "pid": os.getpid(),
            "embed_workers": self._embed_workers,
            "gpu": {
                "provider": gpu_stats["provider"],
                "is_gpu": gpu_stats["is_gpu"],
                "gpu_ops": gpu_stats["gpu_ops"],
                "cpu_ops": gpu_stats["cpu_ops"],
            },
        }
        if is_degraded:
            result["gpu"]["degraded"] = True
            result["gpu"]["degraded_reason"] = gpu_stats.get("degraded_reason", "")
        return result

    def _handle_shutdown(self) -> dict:
        """Graceful shutdown."""
        log.info("shutdown requested")
        self._shutdown.set()
        return {"status": "shutting_down"}

    # ---- HTTP server ----

    def _jsonify(self, v: Any) -> Any:
        """Recursively convert bytes → base64 strings for JSON serialization."""
        if isinstance(v, bytes):
            return base64.b64encode(v).decode("ascii")
        if isinstance(v, dict):
            return {k: self._jsonify(val) for k, val in v.items()}
        if isinstance(v, list):
            return [self._jsonify(i) for i in v]
        return v

    async def _http_health(self, req: web.Request) -> web.Response:
        self._last_activity = time.monotonic()
        return web.json_response({"result": self._handle_health()})

    async def _http_shutdown(self, req: web.Request) -> web.Response:
        self._last_activity = time.monotonic()
        return web.json_response({"result": self._handle_shutdown()})

    async def _http_embed_passages(self, req: web.Request) -> web.Response:
        self._last_activity = time.monotonic()
        try:
            params = await req.json()
            result = await self._handle_embed_passages(params)
            return web.json_response({"result": result})
        except Exception as exc:
            log.exception("http embed_passages error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def _http_embed_passages_f32(self, req: web.Request) -> web.Response:
        self._last_activity = time.monotonic()
        try:
            params = await req.json()
            result = await self._handle_embed_passages_f32(params)
            return web.json_response({"result": self._jsonify(result)})
        except Exception as exc:
            log.exception("http embed_passages_f32 error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def _http_embed_query(self, req: web.Request) -> web.Response:
        self._last_activity = time.monotonic()
        try:
            params = await req.json()
            result = await self._handle_embed_query(params)
            return web.json_response({"result": result})
        except Exception as exc:
            log.exception("http embed_query error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def _http_embed_query_f32(self, req: web.Request) -> web.Response:
        self._last_activity = time.monotonic()
        try:
            params = await req.json()
            result = await self._handle_embed_query_f32(params)
            return web.json_response({"result": self._jsonify(result)})
        except Exception as exc:
            log.exception("http embed_query_f32 error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def _http_chunk(self, req: web.Request) -> web.Response:
        self._last_activity = time.monotonic()
        try:
            params = await req.json()
            result = await self._handle_chunk(params)
            return web.json_response({"result": result})
        except Exception as exc:
            log.exception("http chunk error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def _http_chunk_file(self, req: web.Request) -> web.Response:
        self._last_activity = time.monotonic()
        try:
            params = await req.json()
            result = await self._handle_chunk(params)
            return web.json_response({"result": result})
        except Exception as exc:
            log.exception("http chunk_file error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def _http_chunk_and_embed(self, req: web.Request) -> web.Response:
        self._last_activity = time.monotonic()
        try:
            params = await req.json()
            result = await self._handle_chunk_and_embed(params)
            return web.json_response({"result": result})
        except Exception as exc:
            log.exception("http chunk_and_embed error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def _http_rerank(self, req: web.Request) -> web.Response:
        self._last_activity = time.monotonic()
        try:
            params = await req.json()
            result = await self._handle_rerank(params)
            return web.json_response({"result": result})
        except Exception as exc:
            log.exception("http rerank error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    def _http_app(self) -> web.Application:
        """Build the aiohttp Application with all REST routes."""
        app = web.Application()
        app.router.add_get("/health", self._http_health)
        app.router.add_post("/shutdown", self._http_shutdown)
        app.router.add_post("/embed/passages", self._http_embed_passages)
        app.router.add_post("/embed/passages_f32", self._http_embed_passages_f32)
        app.router.add_post("/embed/query", self._http_embed_query)
        app.router.add_post("/embed/query_f32", self._http_embed_query_f32)
        app.router.add_post("/embed/chunk", self._http_chunk)
        app.router.add_post("/embed/chunk_file", self._http_chunk_file)
        app.router.add_post("/embed/chunk_and_embed", self._http_chunk_and_embed)
        app.router.add_post("/embed/rerank", self._http_rerank)
        return app

    # ---- Model pre-warming ----

    async def _warmup_models(self) -> None:
        """Pre-load models to avoid first-request latency.

        This runs on startup and pre-downloads/caches:
        - FastEmbed embedding model (ONNX + tokenizer)
        - Common tree-sitter CodeChunkers for popular languages

        The SemanticChunker (potion-base-32M) is NOT pre-warmed because:
        - It adds ~12s startup time and 150MB memory
        - It's only used for plain text files (rare in code repos)
        - First text file will still trigger the load (~2s)
        """
        # Log GPU status banner before warming up
        # Use asyncio.to_thread to avoid blocking the event loop with subprocess calls
        await asyncio.to_thread(self._log_gpu_status)

        log.info("pre-warming models...")
        t_start = time.monotonic()

        # Pre-warm embedding model in thread pool (downloads if needed)
        await asyncio.to_thread(self._warmup_embedder)

        # Pre-warm common CodeChunkers (tree-sitter grammars)
        await asyncio.to_thread(self._warmup_chunkers)

        elapsed = time.monotonic() - t_start
        log.info("models pre-warmed in %.1fs", elapsed)

        # Log final GPU status after models are loaded
        # Use asyncio.to_thread for consistency with other sync helpers
        await asyncio.to_thread(self._log_gpu_status_after_warmup)

    def _log_gpu_status(self) -> None:
        """Log comprehensive GPU status at startup."""
        provider = get_active_provider()
        is_gpu = is_gpu_available()

        log.info("=" * 60)
        log.info("GPU STATUS CHECK")
        log.info("=" * 60)
        log.info("Active provider: %s", provider.upper())
        log.info("GPU available: %s", "YES" if is_gpu else "NO")

        # Try to get ONNX runtime info
        try:
            import onnxruntime as ort

            available = ort.get_available_providers()
            log.info("ONNX available providers: %s", available)

            # Check for ROCm-specific info
            if "ROCMExecutionProvider" in available:
                log.info("ROCm provider detected in ONNX runtime")
                # Try to get ROCm device info
                try:
                    import subprocess

                    result = subprocess.run(
                        ["rocm-smi", "--showproductname"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if result.returncode == 0:
                        for line in result.stdout.strip().split("\n"):
                            if line.strip():
                                log.info("ROCm device: %s", line.strip())
                except Exception:
                    pass

            # Check for CUDA-specific info
            if "CUDAExecutionProvider" in available:
                log.info("CUDA provider detected in ONNX runtime")

        except ImportError:
            log.warning("onnxruntime not installed")
        except Exception as e:
            log.warning("Failed to get ONNX info: %s", e)

        log.info("=" * 60)

    def _log_gpu_status_after_warmup(self) -> None:
        """Log GPU status after models are loaded to confirm actual usage."""
        stats = get_gpu_stats()
        provider = stats["provider"]
        is_gpu = stats["is_gpu"]

        log.info("=" * 60)
        log.info("POST-WARMUP GPU STATUS")
        log.info("=" * 60)
        if is_gpu:
            log.info("SUCCESS: GPU inference is ACTIVE")
            log.info("Provider: %s", provider.upper())
        else:
            log.warning("WARNING: Running on CPU only")
            log.warning("Provider: %s", provider.upper())
            log.warning("GPU acceleration is NOT being used!")
        log.info("=" * 60)

    def _warmup_embedder(self) -> None:
        """Pre-load the budget tier FastEmbed model (most common)."""
        from opencode_embedder.embeddings import embed_passages

        # Only warm up budget tier model to avoid slow startup
        # Other tiers load on-demand (~1-2s extra on first use)
        model = "jinaai/jina-embeddings-v2-small-en"
        dims = 512
        try:
            embed_passages(["warmup"], model=model, dimensions=dims)
            log.info("embedding model loaded: %s", model)
        except Exception as e:
            log.warning("failed to pre-warm embedder %s: %s", model, e)

    def _warmup_chunkers(self) -> None:
        """Pre-load chunkers to avoid first-request latency."""
        # Pre-warm tree-sitter CodeChunkers for common languages
        languages = [
            "typescript",
            "javascript",
            "python",
            "rust",
            "go",
            "java",
            "cpp",
            "c",
            "ruby",
            "tsx",
        ]
        loaded = 0
        for lang in languages:
            ts_lang = chunker._LANG_TO_TREESITTER.get(lang)
            if ts_lang:
                try:
                    chunker._get_code_chunker(ts_lang)
                    loaded += 1
                except Exception:
                    pass  # grammar not available
        log.info("tree-sitter chunkers loaded: %d/%d languages", loaded, len(languages))

        # Pre-warm SemanticChunker (loads potion-base-32M model for prose/text files)
        try:
            chunker._get_semantic_chunker()
            log.info("semantic chunker loaded (potion-base-32M)")
        except Exception as e:
            log.warning("failed to pre-warm semantic chunker: %s", e)

    # ---- Lifecycle ----

    async def serve(self) -> None:
        """Start the HTTP server on 127.0.0.1."""
        await self._warmup_models()

        # Start idle shutdown monitor (for on-demand spawning support)
        idle_monitor_task = asyncio.create_task(self._idle_shutdown_monitor())

        # Start HTTP server (aiohttp, 127.0.0.1 only)
        http_runner = web.AppRunner(self._http_app())
        await http_runner.setup()
        hp = _http_port()
        http_site = web.TCPSite(http_runner, "127.0.0.1", hp)
        await http_site.start()
        log.info("HTTP server listening on 127.0.0.1:%d", hp)

        profile = "low-end" if _LOW_END else "standard"
        listen_info = f"http://127.0.0.1:{hp}"
        idle_shutdown_info = (
            f", idle_shutdown={self._idle_shutdown_secs}s"
            if self._idle_shutdown_secs > 0
            else ", idle_shutdown=disabled"
        )
        log.info(
            "model server listening on %s (PID %d, %d embed workers, "
            "%d CPUs, %d MB RAM, profile=%s, idle_cleanup=%ds%s)",
            listen_info,
            os.getpid(),
            self._embed_workers,
            _CPUS,
            _RAM_MB,
            profile,
            IDLE_CLEANUP_SECS,
            idle_shutdown_info,
        )
        print(
            f"model-server: listening on {listen_info} "
            f"(PID {os.getpid()}, {self._embed_workers} embed workers, "
            f"{_CPUS} CPUs, {_RAM_MB}MB RAM, {profile}{idle_shutdown_info})",
            flush=True,
        )

        try:
            await self._shutdown.wait()
        except asyncio.CancelledError:
            pass
        finally:
            log.info("shutting down model server")
            await http_runner.cleanup()
            idle_monitor_task.cancel()
            cleanup_models()
            chunker.cleanup_chunkers()
            log.info("model server stopped")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_sub_batches(texts: list[str], size: int) -> list[list[str]]:
    """Split a list of texts into sub-batches of at most `size` items."""
    return [texts[i : i + size] for i in range(0, len(texts), size)]


def _pack_f32_vectors(vectors: list[list[float]]) -> tuple[bytes, int]:
    """Pack vectors into little-endian float32 bytes."""
    if not vectors:
        return b"", 0

    try:
        import numpy as np

        mat = np.asarray(vectors, dtype="<f4")
        if getattr(mat, "ndim", 0) == 2:
            dims = int(mat.shape[1]) if mat.shape[0] else 0
            return mat.tobytes(), dims
    except Exception:
        pass

    import array

    buf = array.array("f")
    for vec in vectors:
        buf.fromlist([float(x) for x in vec])
    if sys.byteorder != "little":
        buf.byteswap()
    return buf.tobytes(), len(vectors[0])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# Default HTTP port for REST API (127.0.0.1 only)
DEFAULT_HTTP_PORT = 9998


def _http_port() -> int:
    """Read OPENCODE_EMBED_HTTP_PORT env var, default 9998."""
    val = os.environ.get("OPENCODE_EMBED_HTTP_PORT", "").strip()
    if not val:
        return DEFAULT_HTTP_PORT
    try:
        return int(val)
    except ValueError:
        return DEFAULT_HTTP_PORT


def _acquire_singleton_lock():
    """Acquire exclusive flock on ~/.opencode/embedder.lock.

    Prevents multiple embedder instances from running simultaneously.
    The lock is held for the process lifetime and released on exit.
    Returns the lock file object (must be kept alive).
    """
    lock_dir = Path.home() / ".opencode"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_dir / "embedder.lock", "w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("another embedder instance is already running (flock held)")
        sys.exit(1)
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file  # must be kept alive


def run_server(
    workers: int | None = None,
    idle_shutdown: int | None = None,
) -> None:
    """Run the HTTP model server (blocking). Called from CLI or standalone.

    Args:
        workers: Number of embed workers (default: auto-detected based on GPU/CPU)
                 Can also be set via OPENCODE_EMBED_WORKERS environment variable.
        idle_shutdown: Seconds of idle time before auto-shutdown (default: 600)
                       Set to 0 to disable. Can also be set via OPENCODE_EMBED_IDLE_SHUTDOWN.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Acquire OS-level singleton lock before anything else.
    # Two simultaneous starts are serialized by the kernel; second one exits.
    _lock = _acquire_singleton_lock()

    # Set up process group for clean child termination (prevents orphaned PIDs)
    _setup_process_group()

    # Determine worker count (CLI arg > env var > auto-detect)
    embed_workers = workers
    if embed_workers is None:
        env_workers = os.environ.get("OPENCODE_EMBED_WORKERS", "").strip()
        if env_workers:
            try:
                embed_workers = int(env_workers)
            except ValueError:
                pass
    if embed_workers is None:
        embed_workers = EMBED_WORKERS  # Auto-detected default

    # Determine idle shutdown timeout (CLI arg > env var > default)
    idle_shutdown_secs = idle_shutdown if idle_shutdown is not None else IDLE_SHUTDOWN_SECS

    srv = ModelServer(embed_workers=embed_workers, idle_shutdown_secs=idle_shutdown_secs)

    loop = asyncio.new_event_loop()

    # Limit default ThreadPoolExecutor used by asyncio.to_thread().
    # Each thread pool worker can spawn OMP_NUM_THREADS internal ONNX threads,
    # so total_threads = pool_size × OMP_threads. To avoid oversubscription:
    #   pool_size = cpu_count / OMP_threads, capped to a reasonable maximum.
    # On a 24-core machine with OMP=4: pool = 24/4 = 6 (not 16!).
    cpus = os.cpu_count() or 4
    omp_threads = int(os.environ.get("OMP_NUM_THREADS", "2"))
    max_pool = max(4, cpus // max(1, omp_threads))
    max_workers = min(max_pool, embed_workers + 4)  # embed + chunk + overhead
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    loop.set_default_executor(executor)
    log.info(
        "thread pool executor configured: max_workers=%d (cpus=%d, omp=%d)",
        max_workers,
        cpus,
        omp_threads,
    )

    def handle_signal(sig: int) -> None:
        log.info("received signal %d", sig)
        srv._shutdown.set()

    # Handle SIGINT, SIGTERM, and SIGHUP for graceful shutdown
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            loop.add_signal_handler(sig, handle_signal, sig)
        except (ValueError, OSError):
            pass  # Signal may not be available on all platforms

    try:
        loop.run_until_complete(srv.serve())
    finally:
        # Clean up thread pool executor
        executor.shutdown(wait=False)
        loop.close()
        # Ensure all child processes are cleaned up
        _kill_process_group()


if __name__ == "__main__":
    run_server()
