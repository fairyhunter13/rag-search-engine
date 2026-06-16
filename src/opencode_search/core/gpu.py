"""GPU guard: enforce CUDA-only inference. CPU fallback is fatal, never silent."""
from __future__ import annotations

import json
import sys
import urllib.request

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


def assert_ollama_gpu(model: str, base_url: str = "http://127.0.0.1:11434") -> None:
    """Raise RuntimeError if the ollama model is not fully resident on GPU.

    Uses GET /api/ps: size_vram must equal size.  Absent model == unconfirmed == fatal.
    """
    req = urllib.request.Request(f"{base_url}/api/ps",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        raise RuntimeError(f"Cannot reach ollama /api/ps at {base_url}: {exc}") from exc
    for entry in data.get("models", []):
        if entry.get("name") == model or entry.get("model") == model:
            size = entry.get("size", 0)
            size_vram = entry.get("size_vram", 0)
            if size_vram < size or size_vram == 0:
                raise RuntimeError(
                    f"Ollama model '{model}' has CPU layers "
                    f"(size_vram={size_vram} < size={size}). CPU inference is forbidden."
                )
            return
    raise RuntimeError(
        f"Ollama model '{model}' not resident — cannot confirm GPU placement. "
        "CPU inference is forbidden."
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
