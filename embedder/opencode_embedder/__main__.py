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

# Disable tokenizers parallelism to prevent deadlock on macOS
# This must be set before importing any module that uses tokenizers
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# Limit CPU thread usage to prevent CPU/memory hogging.
# ONNX Runtime, MKL, and OpenBLAS each spawn OMP_NUM_THREADS internal threads.
# Default to 2 to keep total thread count bounded.
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("ORT_NUM_THREADS", "2")

# HuggingFace Hub: disable XetHub-backed downloads by default.
# This improves reliability on some networks and avoids CAS/Xet-specific failures.
# Users can override by explicitly setting HF_HUB_DISABLE_XET=0.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


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
        from opencode_embedder.embeddings import _get_onnx_providers
        import onnxruntime as ort

        available = ort.get_available_providers()
        print(f"Available ONNX providers: {available}")

        providers = _get_onnx_providers()
        if providers:
            print(f"Selected providers: {providers}")
            if "ROCMExecutionProvider" in providers:
                print("ROCMExecutionProvider: working")
            elif "CUDAExecutionProvider" in providers:
                print("CUDAExecutionProvider: working")
            else:
                print(f"Using: {providers[0]}")
        else:
            print("No GPU provider available, using CPU")
        raise SystemExit(0)

    if "--help" in sys.argv or "-h" in sys.argv:
        print(
            """OpenCode Model Server - GPU-accelerated embeddings, chunking, and reranking

Usage:
    opencode-embedder                     # Start HTTP server (auto-detected port)
    opencode-embedder --workers 8         # Set number of parallel embed workers

Options:
    --workers N          Number of parallel embed workers (default: auto-detected)
    --idle-shutdown N    Seconds of idle time before auto-shutdown (default: 600, 0 to disable)
    --check-gpu          Check GPU provider availability and exit
    --version            Print version and exit
    --help, -h           Show this help message

Environment variables:
    OPENCODE_EMBED_WORKERS=N            Set number of embed workers (same as --workers)
    OPENCODE_EMBED_IDLE_SHUTDOWN=N      Set idle shutdown timeout (same as --idle-shutdown)
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

    from opencode_embedder.server import run_server

    run_server(workers, idle_shutdown)
