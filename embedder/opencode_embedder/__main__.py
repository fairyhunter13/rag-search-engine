"""Entry point for the OpenCode Python model server.

This package only provides the global singleton model server used by the Rust
indexer (embeddings, reranking, and chunking).

Usage:
    python -m opencode_embedder
    python -m opencode_embedder --workers 8
"""

import multiprocessing
import os
import sys

# Required for PyInstaller-bundled apps on macOS/Windows.
# Must be called before any multiprocessing operations.
multiprocessing.freeze_support()

# Tune glibc malloc BEFORE any significant allocation.
# MALLOC_ARENA_MAX=2: limit per-thread arenas (default is 8×cores = 192 on 24-core)
# MALLOC_TRIM_THRESHOLD_=0: auto-trim freed pages to OS
# MALLOC_MMAP_THRESHOLD_=65536: use mmap for allocs >64K (auto-returned on free)
os.environ.setdefault("MALLOC_ARENA_MAX", "2")
os.environ.setdefault("MALLOC_TRIM_THRESHOLD_", "0")
os.environ.setdefault("MALLOC_MMAP_THRESHOLD_", "65536")

# CUDA: defer kernel loading until first use (saves ~30-50MB pinned memory at startup)
os.environ.setdefault("CUDA_MODULE_LOADING", "LAZY")

# Disable tokenizers parallelism to prevent deadlock on macOS
# This must be set before importing any module that uses tokenizers
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# CPU thread limits are set by embeddings._limit_onnx_threads() which detects
# GPU availability and scales appropriately (1 thread for GPU mode, 2-4 for CPU).
# Do NOT hardcode OMP_NUM_THREADS here — it prevents GPU-aware optimization.
os.environ.setdefault("ORT_NUM_THREADS", "1")

# HuggingFace Hub: disable XetHub-backed downloads by default.
# This improves reliability on some networks and avoids CAS/Xet-specific failures.
# Users can override by explicitly setting HF_HUB_DISABLE_XET=0.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

# Load GPU configuration from ~/.config/opencode/gpu.conf if it exists.
# This allows persistent GPU provider selection across restarts.
# Format: bash-style exports (e.g., export OPENCODE_ONNX_PROVIDER=cuda)
_gpu_conf_path = os.path.expanduser("~/.config/opencode/gpu.conf")
if os.path.exists(_gpu_conf_path):
    try:
        with open(_gpu_conf_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Parse "export KEY=VALUE" format
                if line.startswith("export "):
                    line = line[7:]  # Remove "export " prefix
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    os.environ.setdefault(key, val)
    except Exception:
        pass  # Silently ignore if file is malformed or unreadable


def _version() -> str:
    try:
        from opencode_embedder._version import __version__

        return __version__
    except Exception:
        return "dev"


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(_version())
        raise SystemExit(0)

    if "--check-gpu" in sys.argv:
        # Check GPU provider availability and test if it actually works
        from opencode_embedder.embeddings import _get_onnx_providers, _onnx_available_providers

        available = _onnx_available_providers()
        print(f"Available ONNX providers : {available}")

        providers = _get_onnx_providers()
        if providers:
            gpu_set = {
                "TensorrtExecutionProvider", "CUDAExecutionProvider",
                "ROCMExecutionProvider", "MIGraphXExecutionProvider",
                "DirectMLExecutionProvider", "CoreMLExecutionProvider",
            }
            active_gpu = [p for p in providers if p in gpu_set]
            print(f"Selected providers       : {providers}")
            print(f"Active GPU providers     : {active_gpu or ['(none u2014 CPU only)']}")  
            if active_gpu:
                print(f"Status                   : GPU OK u2014 {active_gpu[0]}")
            else:
                print("Status                   : WARNING u2014 CPU only (no GPU provider active)")
        else:
            print("Selected providers       : (none u2014 ONNX defaults)")
            print("Status                   : WARNING u2014 CPU only")
        raise SystemExit(0 if (providers and any(
            p in {"TensorrtExecutionProvider","CUDAExecutionProvider",
                  "ROCMExecutionProvider","MIGraphXExecutionProvider",
                  "DirectMLExecutionProvider","CoreMLExecutionProvider"}
            for p in providers
        )) else 1)

    if "--help" in sys.argv or "-h" in sys.argv:
        print(
            """OpenCode Model Server - GPU-accelerated embeddings, chunking, and reranking

Usage:
    opencode-embedder                     # Start HTTP server (auto-detected port)
    opencode-embedder --workers 8         # Set number of parallel embed workers

Options:
    --workers N          Number of parallel embed workers (default: auto-detected)
    --idle-shutdown N    Seconds of idle time before auto-shutdown (default: 300, 0 to disable)
    --check-gpu          Check GPU provider availability and exit
    --version            Print version and exit
    --help, -h           Show this help message

Environment variables:
    OPENCODE_EMBED_WORKERS=N            Set number of embed workers (same as --workers)
    OPENCODE_EMBED_IDLE_SHUTDOWN=N      Set idle shutdown timeout (same as --idle-shutdown)
    OPENCODE_GPU_REQUIRED=1             Fail fast at startup if no GPU provider works; no CPU fallback
    OPENCODE_ONNX_PROVIDER=cuda         Force a specific provider (cuda, rocm, cpu, ...)
    OPENCODE_DISABLE_TENSORRT=1         Skip TensorRT (use for RTX 5080 / Blackwell)
    CUDA_VISIBLE_DEVICES=0              Which GPU to use (default: 0)

Exit codes (--check-gpu):
    0  GPU provider found and working
    1  No GPU provider available
"""
        )
        raise SystemExit(0)

    workers = None
    idle_shutdown = None

    # Parse --workers option
    if "--workers" in sys.argv:
        idx = sys.argv.index("--workers")
        if idx + 1 < len(sys.argv):
            try:
                workers = int(sys.argv[idx + 1])
                if workers <= 0:
                    raise ValueError("workers must be positive")
            except ValueError as e:
                print(f"Error: --workers requires a positive integer: {e}", file=sys.stderr)
                raise SystemExit(1)
        else:
            print("Error: --workers requires a number argument", file=sys.stderr)
            raise SystemExit(1)

    # Parse --idle-shutdown option
    if "--idle-shutdown" in sys.argv:
        idx = sys.argv.index("--idle-shutdown")
        if idx + 1 < len(sys.argv):
            try:
                idle_shutdown = int(sys.argv[idx + 1])
                if idle_shutdown < 0:
                    raise ValueError("idle-shutdown must be non-negative")
            except ValueError as e:
                print(
                    f"Error: --idle-shutdown requires a non-negative integer: {e}", file=sys.stderr
                )
                raise SystemExit(1)
        else:
            print("Error: --idle-shutdown requires a number argument", file=sys.stderr)
            raise SystemExit(1)

    # Fail fast: assert GPU is available before spawning workers.
    # When OPENCODE_GPU_REQUIRED=1 this raises GPUNotAvailableError immediately
    # with a clear diagnostic instead of silently degrading to CPU.
    from opencode_embedder.embeddings import assert_gpu_available
    assert_gpu_available()

    from opencode_embedder.server import run_server

    run_server(workers, idle_shutdown)
