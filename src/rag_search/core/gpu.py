"""GPU auto-detect: rank best compatible EP, pick best physical device; CPU is fatal."""
from __future__ import annotations

import os
import sys
from functools import lru_cache

# Ordered preference ladder — best EP first.  CPU is never a valid target.
# NvTensorRTRTXExecutionProvider: Ampere+/Blackwell plugin (not installed here).
# TensorrtExecutionProvider: Blackwell sm_120 incompatible — dropped when DISABLE_TENSORRT=1.
_GPU_EP_ORDER: list[str] = [
    "NvTensorRTRTXExecutionProvider",
    "TensorrtExecutionProvider",
    "CUDAExecutionProvider",
    "MIGraphXExecutionProvider",
    "ROCMExecutionProvider",
    "DmlExecutionProvider",
]
GPU_EP_NAMES: frozenset[str] = frozenset(_GPU_EP_ORDER)
_ARENA_OPTIONS: dict[str, str] = {"arena_extend_strategy": "kSameAsRequested"}
_DLLS_LOADED = False


def rank_gpu_providers(
    available: list[str],
    *,
    disable_tensorrt: bool = False,
) -> list[str]:
    """Return GPU EPs from *available* in ladder order; never includes CPU.

    Pure function — safe for unit-testing with real EP name strings.
    disable_tensorrt drops TensorrtExecutionProvider (required for Blackwell sm_120).
    """
    return [
        ep for ep in _GPU_EP_ORDER
        if ep in available
        and not (disable_tensorrt and ep == "TensorrtExecutionProvider")
    ]


@lru_cache(maxsize=1)
def select_gpu_device() -> int:
    """Pick the best physical GPU (most free VRAM; tie-break: compute capability).

    Sets CUDA_DEVICE_ORDER=PCI_BUS_ID before any CUDA init so NVML index ==
    ORT/CUDA device_id (onnxruntime #26705/#17546).
    Honors RSE_GPU_DEVICE override; falls back to 0 when pynvml unavailable.
    """
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    env_dev = os.environ.get("RSE_GPU_DEVICE")
    if env_dev is not None:
        return int(env_dev)
    try:
        import pynvml
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetDeviceCount()
        if count <= 1:
            return 0
        best_idx, best_free, best_cc = 0, -1, (-1, -1)
        for i in range(count):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            free = pynvml.nvmlDeviceGetMemoryInfo(h).free
            try:
                cc = pynvml.nvmlDeviceGetCudaComputeCapability(h)
            except Exception:
                cc = (0, 0)
            if free > best_free or (free == best_free and cc > best_cc):
                best_free, best_cc, best_idx = free, cc, i
        return best_idx
    except Exception:
        return 0


def select_gpu_providers(
    *,
    disable_tensorrt: bool | None = None,
) -> list[tuple[str, dict]]:
    """Return best available GPU EP(s) as (name, options) tuples; fatal if none."""
    global _DLLS_LOADED
    try:
        import onnxruntime as ort
    except ImportError:
        raise RuntimeError("FATAL: onnxruntime not installed") from None
    if not _DLLS_LOADED:
        ort.preload_dlls()
        _DLLS_LOADED = True
    from rag_search.core.config import DISABLE_TENSORRT
    if disable_tensorrt is None:
        disable_tensorrt = bool(DISABLE_TENSORRT)
    ranked = rank_gpu_providers(ort.get_available_providers(), disable_tensorrt=disable_tensorrt)
    if not ranked:
        raise RuntimeError(
            f"FATAL: no GPU execution provider available "
            f"(found: {ort.get_available_providers()}, disable_tensorrt={disable_tensorrt}). "
            "CPU inference is forbidden."
        )
    device_id = select_gpu_device()
    result: list[tuple[str, dict]] = []
    for ep in ranked:
        opts: dict = {"device_id": device_id}
        if ep in {"NvTensorRTRTXExecutionProvider", "TensorrtExecutionProvider", "CUDAExecutionProvider"}:
            opts.update(_ARENA_OPTIONS)
        result.append((ep, opts))
    return result


def vram_free_mb() -> float:
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(select_gpu_device())
        return pynvml.nvmlDeviceGetMemoryInfo(h).free / 1_048_576
    except Exception:
        return 0.0


def gpu_temp_c() -> float:
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(select_gpu_device())
        return float(pynvml.nvmlDeviceGetTemperature(h, 0))
    except Exception:
        return 0.0


def assert_gpu_available() -> None:
    """Exit with error if no GPU EP is available — CPU fallback is prohibited."""
    try:
        select_gpu_providers()
    except RuntimeError as exc:
        sys.exit(str(exc))


def is_gpu_available() -> bool:
    """Non-fatal GPU check — for CLI health-status display."""
    try:
        return bool(select_gpu_providers())
    except Exception:
        return False

