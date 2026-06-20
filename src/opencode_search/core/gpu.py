"""GPU guard: enforce CUDA-only inference (embeddings + reranking). CPU fallback is fatal."""
from __future__ import annotations

import sys

_DLLS_LOADED = False


def assert_cuda_available() -> None:
    """Exit with error if CUDA EP is unavailable — CPU fallback is prohibited."""
    global _DLLS_LOADED
    try:
        import onnxruntime as ort
    except ImportError:
        sys.exit("FATAL: onnxruntime not installed")
    if not _DLLS_LOADED:
        ort.preload_dlls()  # load nvidia-* wheel .so files (curand/cudnn/cublas) into process
        _DLLS_LOADED = True
    providers = ort.get_available_providers()
    if "CUDAExecutionProvider" not in providers:
        sys.exit(
            f"FATAL: CUDAExecutionProvider not available (found: {providers}). "
            "CPU inference is forbidden on this system."
        )



def is_cuda_available() -> bool:
    try:
        import onnxruntime as ort
        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:
        return False


def vram_free_mb() -> float:
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        return pynvml.nvmlDeviceGetMemoryInfo(h).free / 1_048_576
    except Exception:
        return 0.0


def gpu_temp_c() -> float:
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        return float(pynvml.nvmlDeviceGetTemperature(h, 0))
    except Exception:
        return 0.0
