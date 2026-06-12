"""Local embedding + rerank backend (free).

Implements embeddings and reranking using open-source models via FastEmbed.

Notes:
- Models are downloaded and cached locally by FastEmbed on first use.
- No provider API keys are required.
- Models are unloaded when idle to free ~1-2 GB of RAM per project.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import gc
import logging
import math
import os
import threading

# Configure CUDA/cuDNN library paths before any CUDA-linked import.
# This is the in-code equivalent of the nvidia_ld_fix.pth site hook.
from opencode_search.cuda_setup import configure_cuda_paths as _configure_cuda_paths

_configure_cuda_paths()

log = logging.getLogger(__name__)

from opencode_search.config import DEFAULT_DIMS, DEFAULT_EMBED_MODEL, DEFAULT_RERANK_MODEL

# Check numpy availability once at module load (not in hot path)
_HAS_NUMPY = True
try:
    import numpy as np
except ImportError:
    _HAS_NUMPY = False
    np = None  # type: ignore[assignment]

# CuPy is loaded lazily because importing it eagerly can destabilize
# ONNX Runtime CUDA inference on some Blackwell systems.
_cp = None
_cupy_import_attempted = False


def _get_cupy():
    """Import CuPy on demand.

    Keeping this lazy avoids process-wide CUDA side effects during normal
    embedding/reranking startup, where ONNX Runtime should initialize first.
    """
    global _cp, _cupy_import_attempted
    if _cupy_import_attempted:
        return _cp
    _cupy_import_attempted = True
    try:
        import cupy as cp_mod

        _cp = cp_mod
    except ImportError:
        _cp = None
    return _cp


def _cupy_available() -> bool:
    return _get_cupy() is not None

# GPU normalization mode: "auto" (default), "gpu", "cpu"
_GPU_NORMALIZE_MODE = os.environ.get("OPENCODE_GPU_NORMALIZE", "gpu").lower()

_GPU_PROVIDERS: set[str] = {
    "CUDAExecutionProvider",
    "TensorrtExecutionProvider",
    "ROCMExecutionProvider",
    "MIGraphXExecutionProvider",
    "DirectMLExecutionProvider",
    "CoreMLExecutionProvider",
    # Short names (from get_active_provider()) — single source of truth
    "cuda", "tensorrt", "rocm", "migraphx", "directml", "coreml",
}


def _resize_matrix(mat, dimensions):
    """Resize matrix columns to exactly `dimensions` by truncating or zero-padding."""
    if mat.shape[1] > dimensions:
        return mat[:, :dimensions]
    if mat.shape[1] < dimensions:
        tmp = np.zeros((mat.shape[0], dimensions), dtype=np.float32)
        tmp[:, : mat.shape[1]] = mat
        return tmp
    return mat

_ops_lock = threading.Lock()
_gpu_ops_count = 0
_cpu_ops_count = 0

# Track GPU degradation: True when GPU provider was expected but model fell back to CPU
_gpu_degraded = False
_gpu_degraded_reason: str = ""


def _increment_gpu_ops():
    global _gpu_ops_count
    with _ops_lock:
        _gpu_ops_count += 1


def _increment_cpu_ops():
    global _cpu_ops_count
    with _ops_lock:
        _cpu_ops_count += 1


# GPU capabilities cache (lazy, thread-safe)
_caps: dict | None = None
_caps_done = False
_caps_lock = threading.Lock()


def _detect_gpu_capabilities() -> dict:
    """Detect GPU hardware capabilities for tensor core and FP16 support.

    Tries vendor-specific tools in order: NVIDIA → AMD → Intel → Apple → Qualcomm → Generic.

    Returns dict with:
    - has_tensor_cores: True if GPU has tensor cores (NVIDIA Volta+, AMD MI100+, Intel Arc)
    - compute_capability: NVIDIA SM version string (e.g. "7.0") or None
    - supports_fp16: True if GPU supports efficient FP16
    - vram_mb: Available VRAM in MB or None
    - vendor: "nvidia", "amd", "intel", "apple", "qualcomm", or "unknown"
    - gpu_name: Human-readable GPU name (e.g. "NVIDIA RTX 4090") or None
"""
    import platform
    import subprocess

    result: dict = {
        "has_tensor_cores": False,
        "compute_capability": None,
        "supports_fp16": False,
        "vram_mb": None,
        "vendor": "unknown",
        "gpu_name": None,
        "architecture": None,
    }

    # --- NVIDIA ---
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=compute_cap,memory.total,name",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            line = out.stdout.strip().split("\n")[0]
            parts = line.split(",")
            if len(parts) >= 2:
                cap = parts[0].strip()
                vram = int(parts[1].strip())
                result["compute_capability"] = cap
                result["vram_mb"] = vram
                result["vendor"] = "nvidia"
                if len(parts) >= 3:
                    result["gpu_name"] = parts[2].strip()
                # Tensor cores by architecture:
                # SM 7.0-7.5: Volta/Turing - 1st/2nd gen tensor cores
                # SM 8.0-8.9: Ampere/Ada - 3rd/4th gen tensor cores
                # SM 9.0: Hopper - FP8 tensor cores
                # SM 10.0-10.3: Blackwell data center
                # SM 11.0: Jetson Thor
                # SM 12.0+: Blackwell consumer (RTX 50 series)
                try:
                    major = float(cap)
                except ValueError:
                    major = 0.0
                if major >= 7.0:
                    result["has_tensor_cores"] = True
                    result["supports_fp16"] = True
                if major >= 12.0:
                    result["architecture"] = "blackwell"
                    log.info("Detected NVIDIA Blackwell architecture (SM %.1f)", major)
                elif major >= 8.9:
                    result["architecture"] = "ada"
                elif major >= 8.0:
                    result["architecture"] = "ampere"
                log.info(
                    "NVIDIA GPU: name=%s, compute=%s, vram=%dMB, tensor_cores=%s",
                    result["gpu_name"] or "unknown",
                    cap,
                    vram,
                    result["has_tensor_cores"],
                )
                return result
    except (subprocess.SubprocessError, FileNotFoundError, ValueError, IndexError):
        pass

    # --- AMD ROCm ---
    try:
        out = subprocess.run(
            ["rocm-smi", "--showproductname", "--csv"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            raw = out.stdout
            name = raw.lower()
            vram_mb = _get_gpu_vram_mb()
            result["vram_mb"] = vram_mb
            result["vendor"] = "amd"
            # Parse card series from CSV (header: device,Card series,...)
            for line in raw.strip().split("\n")[1:]:
                parts = line.split(",")
                if len(parts) >= 2 and parts[1].strip():
                    result["gpu_name"] = parts[1].strip()
                    break
            # MI100+ and RDNA2+ (RX 6000+) support FP16 tensor ops
            tensor_gpus = ["mi100", "mi200", "mi210", "mi250", "mi300", "rx 6", "rx 7"]
            if any(m in name for m in tensor_gpus):
                result["has_tensor_cores"] = True
                result["supports_fp16"] = True
            log.info(
                "AMD GPU: name=%s, vram=%sMB, tensor_cores=%s",
                result["gpu_name"] or "unknown",
                vram_mb,
                result["has_tensor_cores"],
            )
            return result
    except (subprocess.SubprocessError, FileNotFoundError, ValueError):
        pass

    # --- Intel (oneAPI sycl-ls) ---
    log.debug("Attempting Intel GPU detection via sycl-ls")
    try:
        out = subprocess.run(
            ["sycl-ls"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and "intel" in out.stdout.lower():
            result["vendor"] = "intel"
            for line in out.stdout.strip().split("\n"):
                low = line.lower()
                if "intel" in low and any(k in low for k in ("arc", "iris", "uhd", "xe")):
                    result["gpu_name"] = line.strip()
                    break
            if result["gpu_name"] is None:
                result["gpu_name"] = "Intel GPU"
            name_low = result["gpu_name"].lower()
            # Intel Arc (Alchemist) has XMX engines for tensor ops
            if "arc" in name_low or "xe" in name_low:
                result["has_tensor_cores"] = True
            result["supports_fp16"] = True
            log.info("Intel GPU detected via sycl-ls: %s", result["gpu_name"])
            return result
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    # --- Intel via /sys/class/drm on Linux (vendor ID 0x8086) ---
    if platform.system() == "Linux":
        log.debug("Attempting Intel GPU detection via /sys/class/drm")
        try:
            import glob as _glob

            for v in _glob.glob("/sys/class/drm/card*/device/vendor"):
                with open(v) as f:
                    vid = f.read().strip()
                if vid == "0x8086":
                    result["vendor"] = "intel"
                    card = v.replace("/device/vendor", "")
                    try:
                        with open(card + "/device/label") as f:
                            result["gpu_name"] = f.read().strip()
                    except Exception:
                        result["gpu_name"] = "Intel GPU"
                    name_low = result["gpu_name"].lower()
                    if "arc" in name_low or "xe" in name_low:
                        result["has_tensor_cores"] = True
                    result["supports_fp16"] = True
                    log.info("Intel GPU detected via /sys/class/drm: %s", result["gpu_name"])
                    return result
        except Exception:
            pass

    # --- Apple Silicon (macOS) ---
    if platform.system() == "Darwin":
        log.debug("Attempting Apple Silicon detection via system_profiler")
        try:
            out = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.returncode == 0:
                # Scan lines for "Apple M<N>" chip name without regex
                chip_name = None
                for _line in out.stdout.splitlines():
                    _l = _line.strip()
                    if _l.lower().startswith("apple m"):
                        chip_name = _l
                        break
                if chip_name is None:
                    # Fallback: find any token that starts with "Apple M"
                    for _tok in out.stdout.split():
                        if _tok.lower().startswith("applem"):
                            chip_name = _tok
                            break
                if chip_name:
                    result["vendor"] = "apple"
                    result["gpu_name"] = chip_name
                    result["supports_fp16"] = True  # All Apple Silicon supports FP16 via Metal
                    # Extract M-series generation number: "Apple M3 Pro" → "3"
                    _gen = 0
                    _low = chip_name.lower()
                    _m_idx = _low.find(" m")
                    if _m_idx >= 0:
                        _after = _low[_m_idx + 2:]  # text after " m"
                        _digits = ""
                        for _ch in _after:
                            if _ch.isdigit():
                                _digits += _ch
                            else:
                                break
                        _gen = int(_digits) if _digits else 0
                    # M3+ has hardware ray tracing; treat all M-series as capable
                    result["has_tensor_cores"] = _gen >= 3
                    log.info(
                        "Apple Silicon detected: %s, fp16=True, tensor_cores=%s",
                        result["gpu_name"],
                        result["has_tensor_cores"],
                    )
                    return result
        except (subprocess.SubprocessError, FileNotFoundError):
            pass

    # --- Qualcomm Adreno (Windows on ARM) ---
    if platform.system() == "Windows":
        log.debug("Attempting Qualcomm GPU detection via wmic")
        try:
            out = subprocess.run(
                ["wmic", "path", "win32_VideoController", "get", "name"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.returncode == 0:
                text = out.stdout.lower()
                if "qualcomm" in text or "adreno" in text:
                    result["vendor"] = "qualcomm"
                    for line in out.stdout.strip().split("\n")[1:]:
                        low = line.lower()
                        if "qualcomm" in low or "adreno" in low:
                            result["gpu_name"] = line.strip()
                            break
                    name_low = (result["gpu_name"] or "").lower()
                    # Adreno 690+ supports FP16 tensor ops — extract model number via str-ops
                    _adreno_num = 0
                    _a_idx = name_low.find("adreno")
                    if _a_idx >= 0:
                        _after = name_low[_a_idx + len("adreno"):].lstrip()
                        _digits = ""
                        for _ch in _after:
                            if _ch.isdigit():
                                _digits += _ch
                            else:
                                break
                        _adreno_num = int(_digits) if _digits else 0
                    if _adreno_num >= 690 or "adreno 7" in name_low:
                        result["supports_fp16"] = True
                    log.info("Qualcomm GPU detected: %s", result["gpu_name"])
                    return result
        except (subprocess.SubprocessError, FileNotFoundError):
            pass

        # DirectML fallback: PowerShell WMI enumeration
        log.debug("Attempting DirectML/WMI GPU detection via PowerShell")
        try:
            out = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-WmiObject Win32_VideoController | Select-Object -ExpandProperty Name",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip():
                name = out.stdout.strip().split("\n")[0].strip()
                result["gpu_name"] = name
                low = name.lower()
                if "nvidia" in low:
                    result["vendor"] = "nvidia"
                elif "amd" in low or "radeon" in low:
                    result["vendor"] = "amd"
                elif "intel" in low:
                    result["vendor"] = "intel"
                    result["supports_fp16"] = True
                log.info("GPU detected via DirectML/WMI: %s (vendor=%s)", name, result["vendor"])
                return result
        except (subprocess.SubprocessError, FileNotFoundError):
            pass

    # --- Generic Linux: /sys/class/drm vendor ID scan ---
    if platform.system() == "Linux":
        log.debug("Attempting generic GPU detection via /sys/class/drm")
        try:
            import glob as _glob

            _vendor_ids = {
                "0x10de": "nvidia",
                "0x1002": "amd",
                "0x8086": "intel",
                "0x5143": "qualcomm",
            }
            for v in _glob.glob("/sys/class/drm/card*/device/vendor"):
                with open(v) as f:
                    vid = f.read().strip()
                if vid in _vendor_ids:
                    result["vendor"] = _vendor_ids[vid]
                    result["gpu_name"] = f"{_vendor_ids[vid].upper()} GPU"
                    log.debug(
                        "GPU detected via /sys/class/drm: vendor=%s (id=%s)",
                        result["vendor"],
                        vid,
                    )
                    return result
        except Exception:
            pass

    return result


def _get_gpu_capabilities() -> dict:
    """Get GPU capabilities (cached, thread-safe)."""
    global _caps, _caps_done
    if _caps_done:
        return _caps or {}
    with _caps_lock:
        if _caps_done:
            return _caps or {}
        _caps = _detect_gpu_capabilities()
        _caps_done = True
        return _caps


def _log_gpu_capabilities() -> None:
    """Log GPU capabilities at INFO level (called after provider test passes)."""
    caps = _get_gpu_capabilities()
    log.info(
        "GPU capabilities: vendor=%s, name=%s, compute=%s, vram=%sMB, "
        "tensor_cores=%s, fp16=%s, io_binding=%s",
        caps.get("vendor", "unknown"),
        caps.get("gpu_name", "unknown"),
        caps.get("compute_capability", "unknown"),
        caps.get("vram_mb", "unknown"),
        caps.get("has_tensor_cores", False),
        caps.get("supports_fp16", False),
        _io_binding_active(),
    )


def get_gpu_stats() -> dict:
    """Get GPU usage statistics for debugging."""
    caps = _get_gpu_capabilities() if (_caps_done or is_gpu_available()) else {}
    stats = {
        "gpu_ops": _gpu_ops_count,
        "cpu_ops": _cpu_ops_count,
        "provider": get_active_provider(),
        "is_gpu": is_gpu_available(),
        "tensor_cores": caps.get("has_tensor_cores", False),
        "fp16_enabled": _fp16_active(),
        "io_binding_active": _io_binding_active(),
        "vendor": caps.get("vendor", "unknown"),
        "gpu_name": caps.get("gpu_name"),
    }
    if _gpu_degraded:
        stats["degraded"] = True
        stats["degraded_reason"] = _gpu_degraded_reason
    return stats


def _resize(vec: list[float], dim: int) -> list[float]:
    if dim <= 0:
        return vec
    if len(vec) == dim:
        return vec
    if len(vec) > dim:
        return vec[:dim]
    return vec + [0.0] * (dim - len(vec))


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum((x * x) for x in vec))
    if norm <= 0:
        return vec
    return [x / norm for x in vec]


def _normalize_np(vec):  # type: ignore[no-untyped-def]
    """Normalize vector using numpy (vectorized, hot path optimization)."""
    if not _HAS_NUMPY:
        return vec
    norm = np.linalg.norm(vec)
    if norm > 0:
        return vec / norm
    return vec


def _resize_np(vec, target_dim: int):  # type: ignore[no-untyped-def]
    """Resize vector to target dimensions using numpy (hot path optimization)."""
    if not _HAS_NUMPY:
        return vec
    if len(vec) == target_dim:
        return vec
    if len(vec) < target_dim:
        return np.pad(vec, (0, target_dim - len(vec)), mode="constant")
    return vec[:target_dim]


def _normalize_embeddings_gpu(mat: np.ndarray) -> np.ndarray:
    """GPU-accelerated L2 normalization using CuPy.

    Keeps data on GPU for normalization, avoiding CPU-GPU transfers.
    CPU fallback is forbidden — raises GPUNotAvailableError on any GPU failure
    so the caller can decide explicitly (e.g., via `_normalize_embeddings`'s
    configured mode router) whether CPU is acceptable.
    """
    cp_mod = _get_cupy()
    if cp_mod is None:
        raise GPUNotAvailableError(
            "[GPU-REQUIRED] _normalize_embeddings_gpu called but CuPy is not "
            "importable. CPU fallback is forbidden for the GPU path. Either "
            "install cupy or call _normalize_embeddings() which honors the "
            "OPENCODE_GPU_NORMALIZE configuration."
        )

    # Transfer to GPU, normalize, transfer back. Any failure is fatal — no
    # silent CPU fallback. Callers wanting graceful degradation must route via
    # `_normalize_embeddings()` with OPENCODE_GPU_NORMALIZE=auto|cpu.
    mat_gpu = cp_mod.asarray(mat, dtype=cp_mod.float32)
    try:
        norms = cp_mod.linalg.norm(mat_gpu, axis=1, keepdims=True)
        norms = cp_mod.maximum(norms, cp_mod.float32(1e-12))  # clamp: avoids div-by-zero; where= unsupported on this CuPy build
        cp_mod.divide(mat_gpu, norms, out=mat_gpu)
        result = cp_mod.asnumpy(mat_gpu)
    finally:
        del mat_gpu
    return result


def _normalize_embeddings_cpu(mat: np.ndarray) -> np.ndarray:
    """CPU L2 normalization using numpy.

    In-place normalization to minimize memory copies.
    """
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    np.divide(mat, norms, out=mat, where=norms > 0)
    return mat


def _normalize_embeddings(mat: np.ndarray) -> np.ndarray:
    """Smart L2 normalization: GPU for large batches, CPU for small.

    Mode controlled by OPENCODE_GPU_NORMALIZE env var:
    - "auto" (default): GPU for batches >= 256, CPU otherwise
    - "gpu": Always use GPU (if available)
    - "cpu": Always use CPU
    """
    batch_size = mat.shape[0]

    if _GPU_NORMALIZE_MODE == "gpu":
        if _cupy_available():
            log.debug(f"normalize: {batch_size} embeddings via GPU (forced)")
            return _normalize_embeddings_gpu(mat)
        else:
            log.debug("normalize: GPU requested but CuPy unavailable, using CPU")
            return _normalize_embeddings_cpu(mat)

    elif _GPU_NORMALIZE_MODE == "cpu":
        log.debug(f"normalize: {batch_size} embeddings via CPU (forced)")
        return _normalize_embeddings_cpu(mat)

    else:  # auto mode
        # Use GPU for large batches (>= 256 embeddings)
        # GPU has overhead, only worth it for larger batches
        if _cupy_available() and batch_size >= 256:
            log.debug(f"normalize: {batch_size} embeddings via GPU (auto)")
            return _normalize_embeddings_gpu(mat)
        else:
            log.debug(f"normalize: {batch_size} embeddings via CPU (auto)")
            return _normalize_embeddings_cpu(mat)


# ---------------------------------------------------------------------------
# Two-role embedder cache: "query" (pinned, interactive) vs "passage" (batch)
#
# ARCHITECTURE: the query/search path must NEVER be blocked by indexing/enrichment.
# Single ONNX session shared by query + passage (VRAM can only hold one session
# alongside Ollama models).  Isolation is achieved via:
#   - Cache-first lookup: once warmed up, the session bypasses the CUBLAS cooldown
#     gate so search is never blocked by a passage-path OOM.
#   - _BUILD_INFER_EXECUTOR (single thread) for the passage/indexing path: one
#     persistent cuBLAS handle per process, no handle storm.
#   - Priority gate (yield_to_interactive) so batch work pauses between sub-batches
#     when a search is in flight.
# ---------------------------------------------------------------------------
_embedder_lock = threading.Lock()
_cached_embedder: object | None = None
_cached_embedder_model: str | None = None

# Process-wide GPU inference serialization lock.
# The RTX 5080 Laptop SBIOS has no hardware thermal protection; concurrent ONNX
# sessions calling .embed()/.rerank() simultaneously on the same CUDA context
# causes native SEGVs (status=11/SEGV) at temperatures ≥80°C.
# Holding this lock around every actual ONNX inference call serializes GPU access
# so no two threads ever run CUDA kernels at the same time.
_GPU_INFER_LOCK = threading.Lock()

# Single-thread executor for the build/indexing path (passage embeds).
# One persistent thread → one cuBLAS handle per process → no handle storm.
# The query path keeps its own _GPU_INFER_EXECUTOR in search.py.
_BUILD_INFER_EXECUTOR: concurrent.futures.ThreadPoolExecutor = (
    concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="gpu-build"
    )
)

# ---------------------------------------------------------------------------
# Priority GPU admission gate (layer 2)
#
# Interactive ops (search embed, rerank) are HIGH priority — they acquire the
# gate immediately and are never queued behind batch work.
# Batch ops (passage embed between sub-batches) are LOW priority — they yield
# when interactive work is in flight and resume once the GPU is free.
#
# Implementation: a counter of in-flight interactive ops + an asyncio.Event
# that batch waiters poll between sub-batches.
# ---------------------------------------------------------------------------
_interactive_in_flight: int = 0
_interactive_lock = threading.Lock()


def _interactive_start() -> None:
    """Mark start of an interactive (high-priority) GPU op."""
    global _interactive_in_flight
    with _interactive_lock:
        _interactive_in_flight += 1


def _interactive_done() -> None:
    """Mark completion of an interactive GPU op."""
    global _interactive_in_flight
    with _interactive_lock:
        _interactive_in_flight = max(0, _interactive_in_flight - 1)


def interactive_in_flight() -> bool:
    """True when at least one interactive GPU op is running — batch ops should yield."""
    with _interactive_lock:
        return _interactive_in_flight > 0


def yield_to_interactive(timeout_s: float = 0.05) -> None:
    """If interactive ops are in flight, sleep briefly to let them finish.

    Called between sub-batches in the passage/build path so a pending search
    can preempt a long indexing run within one sub-batch boundary (~10-50 ms).
    """
    import time as _t
    if interactive_in_flight():
        _t.sleep(timeout_s)

# Idle inference tracking — used by the daemon cleanup loop to unload models
# after a configurable period of no embed/rerank calls.
import time as _time_module

_last_inference_monotonic: float = 0.0
_inference_time_lock = threading.Lock()


def touch_inference_time() -> None:
    """Record that an embed or rerank call just started."""
    global _last_inference_monotonic
    with _inference_time_lock:
        _last_inference_monotonic = _time_module.monotonic()


def seconds_since_last_inference() -> float:
    """Return seconds elapsed since the last embed/rerank call.

    Returns ``inf`` if inference has never been called in this process
    (models are not yet loaded, so cleanup would be a no-op anyway).
    """
    with _inference_time_lock:
        if _last_inference_monotonic == 0.0:
            return float("inf")
        return _time_module.monotonic() - _last_inference_monotonic


def _cuda_sync_and_empty_cache() -> None:
    """Synchronize CUDA stream and try to free cached GPU memory.

    Called after releasing a model to ensure VRAM is available before
    loading the next model.  Best-effort: silently ignores any errors
    (CUDA may not be available, or the driver may reject the call).
    """
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
    except Exception:
        pass


_reranker_cache_lock = threading.Lock()
_reranker_cache: dict[str, object] = {}   # model_name → reranker instance
_reranker_lru: list[str] = []             # index 0 = most recently used
RERANKER_CACHE_SIZE: int = int(os.environ.get("OPENCODE_RERANKER_CACHE_SIZE", "2"))

# R4: IOBinding confirmation flag for the reranker (set per model load)
_rerank_iobinding_confirmed: bool = False
_rerank_iobinding_lock = threading.Lock()
_rerank_input_names: list[str] = []


def _build_provider_list_with_options(providers: list[str]) -> list:
    """Inject per-provider option dicts into a provider list for ORT.

    ORT accepts providers as either plain strings or (name, options_dict) tuples.
    Injecting options here ensures CUDA memory limits, cudnn algo search, and
    Blackwell compat flags are always applied regardless of list length.

    Bug fix: the old code guarded on `len(providers) >= 2`, which silently
    skipped options when OPENCODE_ONNX_PROVIDER=cuda returned a single-item
    list before GPU provider options were applied.
    """
    result: list = []
    for p in providers:
        if p in _GPU_PROVIDERS:
            opts = _gpu_provider_options(p)
            log.debug("Provider options for %s: %s", p, opts[0])
            result.append((p, opts[0]))
        else:
            result.append(p)
    return result


_fastembed_patch_applied: bool = False


def _fastembed_cache_dir() -> str:
    """Return the permanent FastEmbed model cache directory — never /tmp.

    Honors FASTEMBED_CACHE_PATH if already set, otherwise defaults to the
    persistent ~/.cache/opencode/fastembed (same path daemon.py pins on spawn).
    Also writes the resolved path back into the environment via setdefault so
    any child subprocess (index pipeline, reload) inherits it and never falls
    back to FastEmbed's own default of tempfile.gettempdir()/fastembed_cache
    (/tmp), which is tmpfs and wiped on reboot.
    """
    path = os.environ.get("FASTEMBED_CACHE_PATH") or os.path.expanduser(
        "~/.cache/opencode/fastembed"
    )
    os.environ.setdefault("FASTEMBED_CACHE_PATH", path)
    os.makedirs(path, exist_ok=True)
    return path


def _apply_fastembed_patch() -> None:
    """Monkey-patch FastEmbed to accept extra ORT SessionOptions.

    Called once before the first TextEmbedding/TextCrossEncoder is created.
    Kept lazy (not at module level) so that importing embeddings.py does NOT
    pull in fastembed → onnxruntime → CUDA at import time.  Eager import
    caused SIGSEGV on Blackwell (SM 12.0): two processes sharing CUDA UVM
    (test process + daemon) deadlock or corrupt the heap.
    """
    global _fastembed_patch_applied
    if _fastembed_patch_applied:
        return
    _fastembed_patch_applied = True
    # Pin the cache dir into the env before any subprocess can be spawned so
    # child processes (index pipeline, reload) never land on /tmp.
    _fastembed_cache_dir()
    try:
        from fastembed.common.onnx_model import OnnxModel as _OnnxModel
        if "enable_mem_pattern" not in _OnnxModel.EXPOSED_SESSION_OPTIONS:
            _OnnxModel.EXPOSED_SESSION_OPTIONS = (
                *_OnnxModel.EXPOSED_SESSION_OPTIONS,
                "enable_mem_pattern", "execution_mode", "graph_optimization_level",
                "log_severity_level",
            )
            _orig_add_extra = _OnnxModel.add_extra_session_options.__func__
            @classmethod
            def _patched_add_extra(cls, session_options, extra_options):
                _orig_add_extra(cls, session_options, extra_options)
                if "enable_mem_pattern" in extra_options:
                    session_options.enable_mem_pattern = extra_options["enable_mem_pattern"]
                if "execution_mode" in extra_options:
                    session_options.execution_mode = extra_options["execution_mode"]
                if "graph_optimization_level" in extra_options:
                    session_options.graph_optimization_level = extra_options["graph_optimization_level"]
                if "log_severity_level" in extra_options:
                    session_options.log_severity_level = extra_options["log_severity_level"]
                # Disable weight prepacking: ORT pre-packs MatMul weights into CPU-optimized
                # layouts even when GPU handles inference. Disabling saves ~30-60MB CPU RSS.
                if extra_options.get("enable_cpu_mem_arena") is False:
                    with contextlib.suppress(Exception):
                        session_options.add_session_config_entry("session.disable_prepacking", "1")
            _OnnxModel.add_extra_session_options = _patched_add_extra
            del _patched_add_extra
    except ImportError:
        pass


_ONNX_LOG_SEVERITY_LEVEL = int(os.environ.get("OPENCODE_ONNX_LOG_SEVERITY", "3"))

# ---------------------------------------------------------------------------
# CUBLAS circuit breaker: adaptive retry + Ollama probe + hard cooldown
#
# When Ollama loads qwen3-query:8b it briefly contends for VRAM, causing
# CUBLAS_STATUS_ALLOC_FAILED during ONNX session creation. The old behaviour
# (30s hard block, no retry) meant a single transient OOM locked the daemon
# for a full 30s and lost the request.
#
# New behaviour:
#   1. Attempt fn() up to _CUBLAS_MAX_RETRIES times.
#   2. On each CUBLAS error: probe Ollama /api/ps to wait for model loading
#      to settle, then sleep with exponential backoff before retry.
#   3. Only after all retries are exhausted: enter hard cooldown and re-raise.
# ---------------------------------------------------------------------------
_cublas_fail_time: float = 0.0
_cublas_fail_lock = threading.Lock()
_CUBLAS_COOLDOWN_S: float = float(os.environ.get("OPENCODE_CUBLAS_COOLDOWN_S", "30"))
_CUBLAS_MAX_RETRIES: int = int(os.environ.get("OPENCODE_CUBLAS_MAX_RETRIES", "4"))
_CUBLAS_BACKOFF_BASE_S: float = float(os.environ.get("OPENCODE_CUBLAS_BACKOFF_BASE_S", "1.0"))
_CUBLAS_OLLAMA_PROBE_TIMEOUT_S: float = float(
    os.environ.get("OPENCODE_CUBLAS_OLLAMA_PROBE_TIMEOUT_S", "15")
)
_OLLAMA_PS_URL: str = os.environ.get(
    "OPENCODE_OLLAMA_PS_URL", "http://localhost:11434/api/ps"
)

# Diagnostic counters — read via get_cublas_metrics(); writes are thread-safe
# via _cublas_fail_lock (counters are incremented only in locked sections or
# from a single worker thread, so correctness is maintained).
_cublas_retry_attempts: int = 0
_cublas_retry_recoveries: int = 0
_cublas_hard_cooldowns_entered: int = 0
_cublas_ollama_waits: int = 0


def _is_cublas_error(exc: BaseException) -> bool:
    msg = str(exc)
    return "CUBLAS" in msg or "cublasCreate" in msg or "resource allocation" in msg.lower()


def _probe_ollama_loading(timeout_s: float) -> bool:
    """Poll Ollama /api/ps until no model is actively loading, up to timeout_s.

    Returns True if we waited (i.e. a model was in a loading-like state),
    False if the endpoint was idle or unreachable (no wait needed).
    """
    global _cublas_ollama_waits
    import time as _t
    import urllib.request
    _LOADING_TAGS = ("qwen3", "qwen2", "llama", "mistral", "gemma")
    deadline = _t.monotonic() + timeout_s
    waited = False
    while _t.monotonic() < deadline:
        try:
            with urllib.request.urlopen(_OLLAMA_PS_URL, timeout=2) as resp:
                import json as _json
                data = _json.loads(resp.read())
            models = data.get("models") or []
            loading = [
                m for m in models
                if m.get("size_vram", 0) > 0
                and any(tag in m.get("name", "").lower() for tag in _LOADING_TAGS)
            ]
            if not loading:
                break
            waited = True
            _t.sleep(1.0)
        except Exception:
            break
    if waited:
        with _cublas_fail_lock:
            _cublas_ollama_waits += 1
        log.info("CUBLAS retry: waited for Ollama model loading to settle")
    return waited


def _cublas_call_with_retry(label: str, fn):
    """Run fn() with retry+backoff on transient CUBLAS errors.

    Non-CUBLAS exceptions re-raise immediately on the first attempt.
    After _CUBLAS_MAX_RETRIES exhausted: call _record_cublas_failure() and re-raise.
    """
    global _cublas_retry_attempts, _cublas_retry_recoveries
    import time as _t
    last_exc: BaseException | None = None
    for attempt in range(_CUBLAS_MAX_RETRIES + 1):
        try:
            result = fn()
            if attempt > 0:
                with _cublas_fail_lock:
                    _cublas_retry_recoveries += 1
                log.info(
                    "CUBLAS retry [%s]: recovered after %d attempt(s)", label, attempt + 1
                )
            return result
        except Exception as exc:
            if not _is_cublas_error(exc):
                raise
            last_exc = exc
            if attempt >= _CUBLAS_MAX_RETRIES:
                break
            with _cublas_fail_lock:
                _cublas_retry_attempts += 1
            backoff = _CUBLAS_BACKOFF_BASE_S * (2 ** attempt)
            log.warning(
                "CUBLAS error on %s (attempt %d/%d), probing Ollama then retrying in %.1fs: %s",
                label, attempt + 1, _CUBLAS_MAX_RETRIES, backoff, exc,
            )
            _probe_ollama_loading(_CUBLAS_OLLAMA_PROBE_TIMEOUT_S)
            _t.sleep(backoff)
    _record_cublas_failure()
    raise last_exc  # type: ignore[misc]


def _record_cublas_failure() -> None:
    global _cublas_fail_time, _cublas_hard_cooldowns_entered
    import time as _t
    with _cublas_fail_lock:
        _cublas_fail_time = _t.monotonic()
        _cublas_hard_cooldowns_entered += 1
    log.error(
        "CUBLAS resource allocation failed after %d retries — blocking ONNX session "
        "creation for %.0fs. Free GPU memory (stop Ollama large models) and retry, "
        "or restart the daemon.",
        _CUBLAS_MAX_RETRIES,
        _CUBLAS_COOLDOWN_S,
    )


def _cublas_in_cooldown() -> bool:
    import time as _t
    with _cublas_fail_lock:
        return (_t.monotonic() - _cublas_fail_time) < _CUBLAS_COOLDOWN_S


def get_cublas_metrics() -> dict:
    """Snapshot of CUBLAS circuit-breaker counters for /api/metrics."""
    import time as _t
    with _cublas_fail_lock:
        fail_time = _cublas_fail_time
        in_cd = (_t.monotonic() - fail_time) < _CUBLAS_COOLDOWN_S
        remaining = max(0.0, _CUBLAS_COOLDOWN_S - (_t.monotonic() - fail_time)) if fail_time else 0.0
        return {
            "retry_attempts": _cublas_retry_attempts,
            "retry_recoveries": _cublas_retry_recoveries,
            "hard_cooldowns_entered": _cublas_hard_cooldowns_entered,
            "ollama_waits": _cublas_ollama_waits,
            "in_cooldown": in_cd,
            "cooldown_remaining_s": round(remaining, 1),
        }


def _embedder(model: str, role: str = "passage"):
    """Return (and cache) a FastEmbed TextEmbedding model loaded with GPU providers.

    A single ONNX session is shared by both role="query" and role="passage".
    VRAM can only fit one embedding session alongside the Ollama LLM models on
    this GPU, so creating two sessions causes CUBLAS OOM.

    Isolation guarantee: once warmed up at startup the session is always in the
    hot cache and the CUBLAS cooldown gate is bypassed (cache check runs first).
    The passage/build path is isolated via _BUILD_INFER_EXECUTOR (single thread
    = one cuBLAS handle, no handle storm), and the priority gate
    (yield_to_interactive) pauses batch work when a search is in flight.
    """
    global _cached_embedder, _cached_embedder_model
    if not model:
        model = DEFAULT_EMBED_MODEL

    # --- HOT PATH: cache check BEFORE cooldown gate ---
    # If the session is already resident (normal after warmup_query_models()),
    # return it immediately regardless of cooldown state.  This is the key
    # protection for the query/search path: a passage-path CUBLAS failure sets
    # the cooldown flag but the query path is unaffected because the session
    # object is already in the cache.
    with _embedder_lock:
        if _cached_embedder is not None and _cached_embedder_model == model:
            return _cached_embedder

    # --- COLD LOAD PATH: check cooldown before creating a new session ---
    if _cublas_in_cooldown():
        raise RuntimeError(
            f"ONNX session creation blocked: CUBLAS OOM cooldown ({_CUBLAS_COOLDOWN_S:.0f}s). "
            "Free GPU memory and retry, or restart the daemon."
        )

    with _embedder_lock:
        # Re-check under lock (another thread may have loaded while we waited).
        if _cached_embedder is not None and _cached_embedder_model == model:
            return _cached_embedder

        # Release old model before loading new one.
        # IMPORTANT: ORT holds CUDA memory that Python GC cannot release.
        # We must explicitly delete the old session, run multiple GC passes,
        # and synchronize CUDA to ensure VRAM is fully freed before loading
        # the new model (which can allocate 1-2 GB).  Failing to do this
        # reliably causes CUDA OOM -> process killed -> embedder crash.
        if _cached_embedder is not None:
            old = _cached_embedder
            _cached_embedder = None
            _cached_embedder_model = None
            del old
            gc.collect()
            gc.collect()
            _cuda_sync_and_empty_cache()

        _apply_fastembed_patch()
        from fastembed import TextEmbedding

        providers = _get_onnx_providers()
        log.info(
            "Loading embedding model: %s with providers: %s",
            model,
            providers or "default (CPU)",
        )

        if not providers:
            _raise_no_gpu(
                available=_onnx_available_providers(),
                tested=list(_GPU_PROVIDERS),
            )
        provider_list = _build_provider_list_with_options(providers)

        import onnxruntime as _ort_ref
        embedder = _cublas_call_with_retry(
            "embedder",
            lambda: TextEmbedding(
                model_name=model, providers=provider_list,
                cache_dir=_fastembed_cache_dir(),
                enable_cpu_mem_arena=False, enable_mem_pattern=False,
                execution_mode=_ort_ref.ExecutionMode.ORT_SEQUENTIAL,
                graph_optimization_level=_ort_ref.GraphOptimizationLevel.ORT_ENABLE_EXTENDED,
                log_severity_level=_ONNX_LOG_SEVERITY_LEVEL,
            ),
        )

        _verify_onnx_session_provider(embedder, "embedder")
        with contextlib.suppress(Exception):
            embedder.model.tokenizer.enable_truncation(max_length=_MAX_TOKENS)

        _cached_embedder = embedder
        _cached_embedder_model = model
        return embedder


def _reranker(model: str):
    """Return (and LRU-cache) a FastEmbed TextCrossEncoder loaded with GPU providers.

    Holds up to RERANKER_CACHE_SIZE models simultaneously. Evicts the least recently
    used entry when the cache is full, freeing VRAM before loading the new model.
    """
    global _rerank_iobinding_confirmed, _rerank_input_names
    if not model:
        model = DEFAULT_RERANK_MODEL
    with _reranker_cache_lock:
        # Cache hit: promote to front of LRU list
        if model in _reranker_cache:
            _reranker_lru.remove(model)
            _reranker_lru.insert(0, model)
            return _reranker_cache[model]

        # Cache miss + full cache: evict LRU tail entry
        if len(_reranker_cache) >= RERANKER_CACHE_SIZE:
            evict_model = _reranker_lru.pop()
            old = _reranker_cache.pop(evict_model, None)
            if old is not None:
                del old
                gc.collect()
                gc.collect()
                _cuda_sync_and_empty_cache()
            log.info("reranker LRU evicted: %s", evict_model)

        _apply_fastembed_patch()
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        providers = _get_onnx_providers()
        log.info(
            "Loading reranker model: %s with providers: %s",
            model,
            providers or "default (CPU)",
        )

        if not providers:
            _raise_no_gpu(
                available=_onnx_available_providers(),
                tested=list(_GPU_PROVIDERS),
            )
        provider_list = _build_provider_list_with_options(providers)

        import onnxruntime as _ort_ref
        if _cublas_in_cooldown():
            raise RuntimeError(
                f"Reranker blocked: CUBLAS OOM cooldown ({_CUBLAS_COOLDOWN_S:.0f}s)."
            )
        reranker = _cublas_call_with_retry(
            "reranker",
            lambda: TextCrossEncoder(
                model_name=model, providers=provider_list,
                cache_dir=_fastembed_cache_dir(),
                enable_cpu_mem_arena=False, enable_mem_pattern=False,
                execution_mode=_ort_ref.ExecutionMode.ORT_SEQUENTIAL,
                graph_optimization_level=_ort_ref.GraphOptimizationLevel.ORT_ENABLE_EXTENDED,
                log_severity_level=_ONNX_LOG_SEVERITY_LEVEL,
            ),
        )

        # Verify GPU provider and probe IOBinding for this model
        _verify_onnx_session_provider(reranker, "reranker")
        _verify_reranker_iobinding(reranker, model)

        _reranker_cache[model] = reranker
        _reranker_lru.insert(0, model)
        return reranker


def _verify_reranker_iobinding(reranker_obj, model: str) -> None:
    """Probe IOBinding support and record input names for the reranker session."""
    global _rerank_iobinding_confirmed, _rerank_input_names
    try:
        session = None
        if hasattr(reranker_obj, "model"):
            inner = reranker_obj.model
            if hasattr(inner, "model"):
                session = inner.model
            elif hasattr(inner, "session"):
                session = inner.session
        if session is None:
            return

        names = [inp.name for inp in session.get_inputs()]
        _rerank_input_names = names
        log.info("reranker input names for %s: %s", model, names)

        # Only confirm IOBinding when input names are the standard pair
        if set(names) >= {"input_ids", "attention_mask"}:
            try:
                _probe = session.io_binding()
                for out in session.get_outputs():
                    _probe.bind_output(out.name, "cuda")
                    break
                with _rerank_iobinding_lock:
                    _rerank_iobinding_confirmed = True
                log.info("reranker IOBinding confirmed for model: %s", model)
            except Exception as e:
                with _rerank_iobinding_lock:
                    _rerank_iobinding_confirmed = False
                log.info("reranker IOBinding not supported: %s", e)
        else:
            with _rerank_iobinding_lock:
                _rerank_iobinding_confirmed = False
            log.info("reranker IOBinding skipped (non-standard input names): %s", names)
    except Exception as e:
        log.debug("reranker IOBinding probe failed: %s", e)


def cleanup_models() -> bool:
    """Release cached ONNX models to free VRAM and RAM.

    Models reload on next inference call (~2-5s cost).
    Safe to call from any thread — uses locks internally.

    Returns True if any model was actually released, False if nothing was loaded.
    Callers should use the return value to avoid redundant GC/CUDA-sync calls.
    """
    global _cached_embedder, _cached_embedder_model, _embed_batch_count

    released = False

    with _embedder_lock:
        if _cached_embedder is not None:
            old = _cached_embedder
            _cached_embedder = None
            _cached_embedder_model = None
            del old
            released = True

    with _reranker_cache_lock:
        for model_name in list(_reranker_cache.keys()):
            old_r = _reranker_cache.pop(model_name, None)
            if old_r is not None:
                del old_r
                released = True
        if released:
            _reranker_lru.clear()

    # Reset the Blackwell session-reset counter so the next member starts fresh.
    # Without this, accumulated resets from a large member's indexing carry over
    # and the counter fires too early in the next session.
    with _embed_batch_count_lock:
        _embed_batch_count = 0

    if released:
        gc.collect()
        gc.collect()  # second pass for objects with __del__
        _cuda_sync_and_empty_cache()
        log.info("cleanup_models: released cached embedder and all rerankers")
    else:
        log.debug("cleanup_models: no models loaded, nothing to release")

    return released


def _setup_migraphx_caching() -> None:
    """Enable MIGraphX compiled model caching to avoid recompilation.

    MIGraphX compiles models at runtime for each unique input shape, which can
    take 30-40 seconds per shape. This caching allows reuse across sessions.

    Environment variables used:
    - ORT_MIGRAPHX_SAVE_COMPILED_MODEL: Save compiled models to cache
    - ORT_MIGRAPHX_LOAD_COMPILED_MODEL: Load pre-compiled models from cache
    - ORT_MIGRAPHX_SAVE_MODEL_PATH / ORT_MIGRAPHX_LOAD_MODEL_PATH: Cache directory
    """
    cache_dir = os.path.expanduser("~/.cache/opencode/migraphx")
    os.makedirs(cache_dir, exist_ok=True)

    # Enable save and load of compiled models
    os.environ.setdefault("ORT_MIGRAPHX_SAVE_COMPILED_MODEL", "1")
    os.environ.setdefault("ORT_MIGRAPHX_LOAD_COMPILED_MODEL", "1")
    os.environ.setdefault("ORT_MIGRAPHX_SAVE_MODEL_PATH", cache_dir)
    os.environ.setdefault("ORT_MIGRAPHX_LOAD_MODEL_PATH", cache_dir)

    log.info("MIGraphX model cache directory: %s", cache_dir)


def _limit_onnx_threads() -> None:
    """Set ONNX Runtime thread count based on available CPUs.

    ONNX RT allocates a memory arena per thread. Scale threads to balance
    performance vs contention with multiple embed workers.

    The server runs N embed workers (4-6 for high-end). To avoid CPU contention:
    When GPU is active, OMP defaults to 1 (CPU threads only tokenize).
      total_threads = workers x onnx_threads <= cpu_count

    Scaling (assuming 4-6 workers on high-end):
      low-memory mode: 1 thread (minimise memory arenas)
      <=4 CPUs:  1 thread
      5-8 CPUs: 2 threads (2 workers x 2 = 4-8 threads)
      9-16 CPUs: 2 threads (4 workers x 2 = 8 threads)
      >16 CPUs: 4 threads (6 workers x 4 = 24 threads)
    """
    cpus = os.cpu_count() or 2
    low_memory = os.environ.get("OPENCODE_EMBED_LOW_MEMORY", "").strip() in ("1", "true", "yes")
    # When GPU handles inference, CPU threads are only for tokenization/pre-processing.
    # 1 OMP thread is sufficient — reduces per-thread stack memory (~8MB each).
    gpu_mode = os.environ.get("OPENCODE_ONNX_PROVIDER", "").lower() not in ("cpu", "")
    if not gpu_mode:
        # Auto-detect GPU presence via NVML (management-only, no CUDA UVM init).
        # Using ort.get_available_providers() here would eagerly initialize the CUDA
        # runtime in any process that imports embeddings.py — on Blackwell (SM 12.0)
        # two processes with active CUDA UVM cause cross-process page-fault deadlocks.
        try:
            import pynvml
            pynvml.nvmlInit()
            gpu_mode = pynvml.nvmlDeviceGetCount() > 0
            pynvml.nvmlShutdown()
        except Exception:
            pass
    if low_memory or gpu_mode:
        threads = "1"  # GPU mode: CPU threads only for tokenization, 1 is enough
    elif cpus <= 4:
        threads = "1"
    elif cpus <= 8 or cpus <= 16:
        threads = "2"
    else:
        threads = "4"
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        if gpu_mode:
            os.environ[var] = threads  # Force override: GPU mode needs minimal CPU threads
        else:
            os.environ.setdefault(var, threads)

    # Limit Rust parallelism (if any Rust libraries are used)
    os.environ.setdefault("RAYON_NUM_THREADS", "4")

    # Prevent HuggingFace tokenizer parallelism (avoids threading issues)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    # Disable HuggingFace xet protocol (causes issues on some systems)
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

    # Setup MIGraphX caching (must be done before onnxruntime import)
    _setup_migraphx_caching()


# Apply thread limits before any ONNX import
_limit_onnx_threads()


# Cached provider detection result (computed once at first use)
# Thread-safe via _provider_lock to prevent race conditions during initialization
_detected_providers: list[str] | None = None
_provider_detection_done: bool = False
_provider_lock = threading.Lock()


def _get_onnx_providers() -> list[str] | None:
    """Get optimal ONNX execution providers for this system.

    Automatically detects and tests GPU providers. Raises RuntimeError if no
    working GPU provider is found — CPU fallback is always forbidden.
    Results are cached after first detection. Thread-safe.

    Environment variables:
    - OPENCODE_GPU_REQUIRED: Kept for compatibility; GPU is always required regardless.
    - OPENCODE_ONNX_PROVIDER: Force a specific provider (e.g., "cuda", "rocm", "coreml", "cpu")
    - OPENCODE_ONNX_PROVIDERS: Comma-separated list of providers to try
    - OPENCODE_DISABLE_TENSORRT: If "1", skip TensorRT (Blackwell compat)

    Provider priority (tested in order):
    1. CUDAExecutionProvider: NVIDIA GPUs (stable, all generations)
    2. TensorrtExecutionProvider: NVIDIA TensorRT (if not disabled)
    3. MIGraphXExecutionProvider: AMD GPUs on Linux (ORT 1.23+)
    4. ROCMExecutionProvider: AMD GPUs (older ORT fallback)
    5. DirectMLExecutionProvider: Windows with DirectX 12 GPUs
    6. CPU: NOT AN OPTION — raises RuntimeError if no GPU is found

    Returns:
        List of GPU providers to use. Never returns CPU-only.
    """
    global _detected_providers, _provider_detection_done

    # Fast path: already detected (read without lock is safe for booleans)
    if _provider_detection_done:
        return _detected_providers

    # Slow path: acquire lock for thread-safe detection
    with _provider_lock:
        # Double-check after acquiring lock
        if _provider_detection_done:
            return _detected_providers

        _detected_providers = _detect_and_test_providers()
        _provider_detection_done = True
        return _detected_providers


# Low-memory mode flag (read once at module load)
_LOW_MEMORY_MODE: bool = os.environ.get("OPENCODE_EMBED_LOW_MEMORY", "").strip() in ("1", "true", "yes")


def is_gpu_available() -> bool:
    """Return True if a working GPU provider is available."""
    providers = _get_onnx_providers()
    if providers is None:
        return False
    return any(p in _GPU_PROVIDERS for p in providers)


def get_active_provider() -> str:
    """Return the active provider short name: 'tensorrt', 'cuda', 'rocm', 'directml', 'coreml', 'migraphx', or 'cpu'."""
    providers = _get_onnx_providers()
    if providers is None:
        return "cpu"
    name_map = {
        "TensorrtExecutionProvider": "tensorrt",
        "CUDAExecutionProvider": "cuda",
        "ROCMExecutionProvider": "rocm",
        "MIGraphXExecutionProvider": "migraphx",
        "DirectMLExecutionProvider": "directml",
        "CoreMLExecutionProvider": "coreml",
    }
    for p in providers:
        if p in name_map:
            return name_map[p]
    return "cpu"


def _get_gpu_vram_mb() -> int | None:
    """Return total VRAM in MB for the primary GPU, or None if unknown."""
    caps = _get_gpu_capabilities()
    vram = caps.get("vram_mb")
    return int(vram) if vram else None


# Cached pynvml handle — initialized once to avoid repeated nvmlInit() overhead
_nvml_handle = None
_nvml_available: bool | None = None  # None = not yet probed


def _get_gpu_temp_c() -> int | None:
    """Return GPU temperature in Celsius using pynvml (fast, no subprocess).

    Returns None if pynvml is unavailable or the query fails.
    First call initializes nvml; subsequent calls reuse the cached handle.
    """
    global _nvml_handle, _nvml_available
    if _nvml_available is False:
        return None
    try:
        import pynvml  # type: ignore
        if _nvml_handle is None:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                pynvml.nvmlInit()
            _nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        _nvml_available = True
        return int(pynvml.nvmlDeviceGetTemperature(_nvml_handle, pynvml.NVML_TEMPERATURE_GPU))
    except Exception:
        _nvml_available = False
        _nvml_handle = None
        return None


def _parse_provider_env() -> list[str] | None:
    """Parse provider override from environment variables."""

    # Check for explicit provider list
    providers_env = os.environ.get("OPENCODE_ONNX_PROVIDERS", "").strip()
    if providers_env:
        providers = [p.strip() for p in providers_env.split(",") if p.strip()]
        log.info("Using providers from OPENCODE_ONNX_PROVIDERS: %s", providers)
        return providers

    # Check for single provider shorthand
    provider_env = os.environ.get("OPENCODE_ONNX_PROVIDER", "").strip().lower()
    if not provider_env:
        return None
    if provider_env == "cpu":
        raise GPUNotAvailableError(
            "OPENCODE_ONNX_PROVIDER=cpu is forbidden; opencode-search requires a GPU provider."
        )

    # GPU-only provider map — CPU is not an option.
    provider_map = {
        "tensorrt": ["TensorrtExecutionProvider", "CUDAExecutionProvider"],
        "cuda": ["CUDAExecutionProvider"],
        "rocm": ["ROCMExecutionProvider"],  # Legacy (ORT < 1.23)
        "coreml": ["CoreMLExecutionProvider"],
        "directml": ["DirectMLExecutionProvider"],
        "migraphx": ["MIGraphXExecutionProvider"],  # AMD preferred
        "amd": ["MIGraphXExecutionProvider", "ROCMExecutionProvider"],
        "auto": None,  # Fall through to auto-detection
    }

    if provider_env in provider_map:
        result = provider_map[provider_env]
        if result is not None:
            log.info("Using GPU provider from OPENCODE_ONNX_PROVIDER=%s: %s", provider_env, result)
        return result

    log.warning("Unknown OPENCODE_ONNX_PROVIDER=%s, using auto-detection", provider_env)
    return None


class GPUNotAvailableError(RuntimeError):
    """Raised when no working GPU provider is found. CPU fallback is always forbidden."""


def _raise_no_gpu(available: list[str], tested: list[str]) -> None:
    """Raise GPUNotAvailableError with a clear diagnostic message.

    Called when all GPU providers failed their runtime test.
    CPU fallback is always forbidden.
    """
    msg = (
        "[GPU-REQUIRED] FATAL: No working GPU execution provider was found. "
        "CPU fallback is FORBIDDEN.\n"
        f"  ONNX available providers : {available}\n"
        f"  GPU providers tested     : {tested}\n"
        "\n"
        "Possible causes:\n"
        "  1. CUDA driver not installed or outdated (check: nvidia-smi)\n"
        "  2. onnxruntime-gpu not installed (fix: pip install onnxruntime-gpu)\n"
        "  3. CUDAExecutionProvider not built into this onnxruntime wheel\n"
        "  4. CUDA_VISIBLE_DEVICES='' or set to an invalid device\n"
        "  5. /dev/nvidia* not accessible (check permissions)\n"
        "\n"
        "CPU fallback is not allowed. Fix the GPU driver/library issue.\n"
        "To diagnose: python -c \"import onnxruntime; print(onnxruntime.get_available_providers())\""
    )
    log.critical(msg)
    raise GPUNotAvailableError(msg)


def assert_gpu_available() -> None:
    """Startup check: verify GPU is available and working.

    Call once at process startup so the server fails fast with a clear message
    rather than silently running on CPU. GPU is always required.

    Raises GPUNotAvailableError if no working GPU provider is found OR if CuPy
    (used for GPU-side embedding normalization) is not importable.
    """
    log.info("[GPU-REQUIRED] Startup GPU check")

    # Probe without loading any model — forces provider detection
    providers = _get_onnx_providers()  # may raise GPUNotAvailableError internally

    # Double-check: the returned list must contain at least one GPU provider
    if providers is None or not any(p in _GPU_PROVIDERS for p in providers):
        _raise_no_gpu(
            available=_onnx_available_providers(),
            tested=list(_GPU_PROVIDERS),
        )

    active = next((p for p in (providers or []) if p in _GPU_PROVIDERS), "none")
    log.info("[GPU-REQUIRED] Startup GPU check PASSED — active provider: %s", active)

    # CuPy is required when OPENCODE_GPU_NORMALIZE != "cpu" since the GPU
    # normalization path raises if CuPy is missing. Probe it eagerly so the
    # daemon refuses to start rather than crashing on the first large batch.
    # IMPORTANT: use find_spec (not `import cupy`) — importing cupy here
    # initializes the CUDA runtime in the main thread, which then conflicts
    # with ONNX Runtime's cuBLAS handle created in the GPU worker thread,
    # causing CUBLAS_STATUS_NOT_SUPPORTED (error 7) on the first inference call.
    # find_spec checks that cupy is installed without initialising CUDA.
    if _GPU_NORMALIZE_MODE != "cpu":
        import importlib.util
        if importlib.util.find_spec("cupy") is None:  # pragma: no cover
            msg = (
                "[GPU-REQUIRED] FATAL: CuPy is not installed but "
                f"OPENCODE_GPU_NORMALIZE={_GPU_NORMALIZE_MODE!r} requires it. "
                "Install cupy-cuda12x (or set OPENCODE_GPU_NORMALIZE=cpu to "
                "intentionally use CPU normalization — note this is a configured "
                "choice, not a fallback)."
            )
            log.critical(msg)
            raise GPUNotAvailableError(msg)
        log.info("[GPU-REQUIRED] CuPy check PASSED")


def _onnx_available_providers() -> list[str]:
    """Return onnxruntime's available providers without raising."""
    try:
        import onnxruntime as ort
        return sorted(ort.get_available_providers())
    except Exception:
        return []


def _detect_and_test_providers() -> list[str] | None:
    """Detect available GPU providers and test if they actually work.

    Some providers may be listed as available but fail at runtime due to
    driver mismatches, missing libraries, or model incompatibilities.
    """

    # Check for environment variable override first. Overrides are still
    # validated here; otherwise `health` can report OK for an unavailable
    # provider and only fail later during model load.
    env_providers = _parse_provider_env()
    if env_providers is not None:
        available = _onnx_available_providers()
        accepted: list[str] = []
        for provider in env_providers:
            if provider not in available:
                log.warning("Forced provider %s is not available in ONNX Runtime", provider)
                continue
            if provider in _GPU_PROVIDERS and _test_provider(provider):
                accepted.append(provider)
        if accepted:
            return accepted
        _raise_no_gpu(available=available, tested=env_providers)

    try:
        import onnxruntime as ort
    except ImportError:
        log.warning("onnxruntime not installed, using defaults")
        return None

    available = set(ort.get_available_providers())
    log.info("ONNX available providers: %s", sorted(available))

    # Provider priority order (GPU providers first, then CPU)
    #
    # NVIDIA priority: CUDA > TensorRT (when not explicitly disabled)
    #   - CUDA: proven stable on RTX 4000/5000 series, Blackwell (SM 12.0+)
    #   - TensorRT: kernel fusion benefits, but has driver/MSR compatibility issues
    #     on Blackwell. Disable via OPENCODE_DISABLE_TENSORRT=1 (default for RTX 5080)
    #   - Both can be used in a cascading chain: if TensorRT fails, fall back to CUDA
    #
    # AMD priority: MIGraphX > ROCm
    #   - MIGraphX is AMD's officially recommended provider for ORT 1.23+
    #   - ROCm EP kept as fallback for older ORT versions (< 1.23)
    #   - Users can force ROCm via OPENCODE_ONNX_PROVIDER=rocm if needed
    #
    # Note: CoreML is disabled by default:
    #   - It has a 16384 dimension limit that breaks many embedding models
    #   - It may fail silently and fall back to CPU anyway
    #   - Users can force CoreML via OPENCODE_ONNX_PROVIDER=coreml if needed

    caps = _get_gpu_capabilities()

    # --- TensorRT disable policy ---
    # TensorRT has known MSR/kernel compatibility issues on Blackwell (SM 12.0+).
    # Auto-disable on Blackwell; honour explicit OPENCODE_DISABLE_TENSORRT override.
    tensorrt_disabled_env = os.environ.get("OPENCODE_DISABLE_TENSORRT", "").strip().lower()
    if tensorrt_disabled_env in ("1", "true", "yes"):
        tensorrt_disabled = True
    elif tensorrt_disabled_env in ("0", "false", "no"):
        tensorrt_disabled = False  # explicit opt-in overrides auto-detect
    else:
        # Auto-detect: disable on Blackwell by default
        tensorrt_disabled = caps.get("architecture") == "blackwell"
        if tensorrt_disabled:
            log.info(
                "Blackwell GPU (SM 12.0+) detected: auto-disabled TensorRT "
                "(use OPENCODE_DISABLE_TENSORRT=0 to force-enable)."
            )

    gpu_providers = []

    # Add CUDA first (stable on all NVIDIA generations)
    gpu_providers.append(("CUDAExecutionProvider", "NVIDIA CUDA"))

    # Add TensorRT second (kernel fusion, but conditional on Blackwell compatibility)
    if not tensorrt_disabled:
        gpu_providers.append(("TensorrtExecutionProvider", "NVIDIA TensorRT"))
        log.info("TensorRT enabled; provider chain: CUDA > TensorRT")
    else:
        log.info("TensorRT disabled; provider chain: CUDA-only")

    # Add AMD providers
    gpu_providers.extend([
        ("MIGraphXExecutionProvider", "AMD MIGraphX"),  # Primary for AMD (ORT 1.23+)
        ("ROCMExecutionProvider", "AMD ROCm"),  # Fallback for older ORT (< 1.23)
        ("DirectMLExecutionProvider", "DirectX ML"),
    ])

    # --- Runtime test skip policy ---
    # _test_provider() uses a 129 MB cached model which can fail on Blackwell due to
    # ORT session configuration differences.  For CUDA on SM 8.0+ (Ampere and newer)
    # we can confirm availability more cheaply: if ORT lists CUDAExecutionProvider
    # and nvidia-smi shows a healthy GPU, the provider will work for real workloads.
    # The full verification is deferred to _verify_onnx_session_provider() at model load.
    cc_str = caps.get("compute_capability") or "0"
    try:
        cc_float = float(cc_str)
    except ValueError:
        cc_float = 0.0
    skip_cuda_test = cc_float >= 8.0  # Ampere (SM 8.0) and newer
    if skip_cuda_test:
        log.info(
            "SM %.1f (>= 8.0): skipping CUDA runtime pre-test "
            "(verification deferred to model load)",
            cc_float,
        )

    working_gpu: list[str] = []

    for provider, name in gpu_providers:
        if provider not in available:
            continue

        # CUDA on Ampere/Ada/Blackwell: trust ORT availability, skip slow pre-test
        if provider == "CUDAExecutionProvider" and skip_cuda_test:
            log.info("GPU provider accepted (no pre-test on SM %.1f): %s", cc_float, name)
            _log_gpu_capabilities()
            working_gpu.append(provider)
        elif _test_provider(provider):
            log.info("GPU provider passed test: %s (%s)", provider, name)
            _log_gpu_capabilities()
            working_gpu.append(provider)
        else:
            log.warning("%s available but failed runtime test, skipping", provider)

    if working_gpu:
        log.info("Using GPU-only provider chain: %s", working_gpu)
        return working_gpu

    # No working GPU provider found — raise unconditionally, CPU is not an option.
    _raise_no_gpu(
        available=sorted(available),
        tested=[p for p, _ in gpu_providers],
    )
    return None  # unreachable; satisfies type checker


def _fp16_active() -> bool:
    """Return True if FP16 inference is currently active."""
    if not is_gpu_available():
        return False
    env = os.environ.get("OPENCODE_ONNX_FP16", "auto").lower().strip()
    if env in ("0", "false", "off"):
        return False
    caps = _get_gpu_capabilities()
    if not caps.get("supports_fp16"):
        return False
    if env in ("1", "true", "on"):
        return True
    # auto: check tensor cores
    return caps.get("has_tensor_cores", False)


# Track whether IOBinding has been confirmed working for the active session
_io_binding_confirmed = False
_io_binding_lock = threading.Lock()


def _io_binding_active() -> bool:
    """Return True if IOBinding is active for GPU tensor operations."""
    return _io_binding_confirmed


def _set_io_binding_active(val: bool) -> None:
    global _io_binding_confirmed
    with _io_binding_lock:
        _io_binding_confirmed = val


def _gpu_provider_options(provider: str) -> list[dict]:
    """Build ONNX provider-specific options for GPU providers.

    Returns a list of option dicts (one per provider in the providers list).
    Index 0 = GPU provider options, index 1 = CPU provider options (empty).

    Settings:
    - arena_extend_strategy: kSameAsRequested avoids over-allocation
    - gpu_mem_limit: 80% of detected VRAM to leave headroom
    - cudnn_conv_algo_search: EXHAUSTIVE finds fastest algorithm (NVIDIA only)
    - do_copy_in_default_stream: reduces sync overhead (NVIDIA only)
    """
    caps = _get_gpu_capabilities()

    base: dict = {"arena_extend_strategy": "kSameAsRequested"}
    # Hard cap the ONNX BFC arena at 4 GB (was 3 GB).
    # The old value (80% of VRAM = 12.8 GB) allowed the arena's high-water mark to
    # accumulate across thousands of calls, consuming nearly all VRAM and starving
    # Ollama and other processes. With batch_size=8, peak attention workspace is
    # ~400 MB; 4 GB cap gives models (~800 MB) + workspace (~400 MB) + 2.8 GB headroom.
    _ONNX_ARENA_CAP_MB: int = int(os.environ.get("OPENCODE_ONNX_ARENA_MB", "4096"))
    base["gpu_mem_limit"] = str(_ONNX_ARENA_CAP_MB * 1024 * 1024)

    if provider == "TensorrtExecutionProvider":
        # TensorRT manages its own memory via trt_max_workspace_size;
        # arena_extend_strategy / gpu_mem_limit are CUDA EP options.
        # Use persistent cache dir so TRT engines survive reboots (~3s cold start saved)
        cache = os.path.join(os.path.expanduser("~"), ".cache", "opencode", "trt_cache")
        os.makedirs(cache, exist_ok=True)
        opts: dict = {
            "trt_fp16_enable": "True" if caps.get("supports_fp16") else "False",
            "trt_engine_cache_enable": "True",
            "trt_engine_cache_path": cache,
            # Timing cache persists kernel autotuning results across engine builds
            "trt_timing_cache_enable": "True",
            "trt_timing_cache_path": cache,
            "trt_max_workspace_size": str(2 * 1024 * 1024 * 1024),  # 2GB
        }
        # For Blackwell (SM 12.0+), enable additional optimizations
        if caps.get("architecture") == "blackwell":
            opts["trt_builder_optimization_level"] = "5"  # Maximum optimization
        log.debug("TensorRT provider options: %s", opts)
        return [opts, {}]

    if provider == "CUDAExecutionProvider":
        opts = {
            **base,
            "cudnn_conv_algo_search": "EXHAUSTIVE",
            "do_copy_in_default_stream": "1",
            # CUDA Graphs capture and replay GPU command sequences,
            # eliminating kernel launch overhead (~10-20% latency reduction)
            "enable_cuda_graph": "1",
        }
        cc = caps.get("compute_capability")
        if cc:
            try:
                cc_float = float(cc)
                if cc_float >= 12.0:
                    # Blackwell (SM 12.0+): disable CUDA Graphs (ORT 1.25 + CUDA 13 deadlocks)
                    # and use DEFAULT conv algo to avoid workspace cache accumulation.
                    # NOTE: prefer_nhwc removed — NHWC is for CNNs; transformer MatMul ops
                    # (jina-embeddings, BERT) get CUBLAS_STATUS_NOT_SUPPORTED (7) on Blackwell
                    # when NHWC is active because the cuBLAS GEMM path doesn't support NHWC
                    # input layout for these 2D attention projections.
                    del opts["enable_cuda_graph"]
                    opts["cudnn_conv_algo_search"] = "DEFAULT"
                    log.info(
                        "Blackwell GPU (SM %.1f): disabled CUDA Graphs, DEFAULT conv algo (ORT compat)",
                        cc_float,
                    )
            except ValueError:
                pass
        log.debug("CUDA provider options: %s", opts)
        return [opts, {}]

    if provider == "ROCMExecutionProvider":
        log.debug("ROCm provider options: %s", base)
        return [base, {}]

    if provider == "MIGraphXExecutionProvider":
        # MIGraphX uses 'True'/'False' strings, not '1'/'0'
        # Note: migraphx_mem_limit is NOT supported in ORT 1.22.x
        opts: dict = {}
        if caps.get("supports_fp16"):
            opts["migraphx_fp16_enable"] = "True"
        # device_id is commonly supported
        opts["device_id"] = "0"
        log.debug("MIGraphX provider options: %s", opts)
        return [opts, {}]

    if provider == "DirectMLExecutionProvider":
        # DirectML covers Intel Arc, Qualcomm Adreno, and other DX12 GPUs on Windows
        log.debug("DirectML provider options: %s", base)
        return [base, {}]

    return [{}, {}]


def _device_for_provider(provider: str) -> str:
    """Map ONNX provider name to IOBinding device string."""
    # CUDA, TensorRT, ROCm, and MIGraphX all use "cuda" device in ORT
    if provider in (
        "CUDAExecutionProvider",
        "TensorrtExecutionProvider",
        "ROCMExecutionProvider",
        "MIGraphXExecutionProvider",
    ):
        return "cuda"
    if provider == "DirectMLExecutionProvider":
        return "dml"
    return "cpu"



def _embed_batch_iobinding(
    session,
    tokenizer,
    texts: list[str],
    batch_size: int,
    device: str = "cuda",
    device_id: int = 0,
) -> np.ndarray | None:
    """Embed a batch using IOBinding to keep tensors on GPU.

    Processes texts in chunks of batch_size to bound peak VRAM usage — the
    Jina v2 family supports sequences up to 8192 tokens via ALiBi attention,
    so a naïve single-shot inference over a large sub-batch (e.g. 128 texts ×
    1024 tokens) triggers a 4 GiB Q×K^T allocation and OOM-kills the process.

    Returns numpy array of embeddings (single GPU→CPU copy per chunk) or None
    if IOBinding fails on any chunk.
    """
    try:
        import onnxruntime as ort

        input_names = {node.name for node in session.get_inputs()}
        output_names = [o.name for o in session.get_outputs()]
        all_results: list[np.ndarray] = []

        for start in range(0, len(texts), batch_size):
            chunk = texts[start : start + batch_size]

            # Tokenize on CPU (fast, unavoidable)
            encoded = tokenizer.encode_batch(chunk)
            input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
            attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)

            # Create IOBinding for GPU tensors
            binding = session.io_binding()

            # Bind inputs to GPU
            input_ids_gpu = ort.OrtValue.ortvalue_from_numpy(input_ids, device, device_id)
            attention_mask_gpu = ort.OrtValue.ortvalue_from_numpy(attention_mask, device, device_id)

            binding.bind_ortvalue_input("input_ids", input_ids_gpu)
            binding.bind_ortvalue_input("attention_mask", attention_mask_gpu)

            token_type_ids_gpu = None
            if "token_type_ids" in input_names:
                token_type_ids = np.array(
                    [getattr(item, "type_ids", [0] * len(item.ids)) for item in encoded],
                    dtype=np.int64,
                )
                token_type_ids_gpu = ort.OrtValue.ortvalue_from_numpy(
                    token_type_ids, device, device_id
                )
                binding.bind_ortvalue_input("token_type_ids", token_type_ids_gpu)

            # Bind output to GPU (avoids CPU allocation)
            for name in output_names:
                binding.bind_output(name, device)

            # Run inference with IOBinding (tensors stay on GPU)
            session.run_with_iobinding(binding)

            # Extract output (single GPU→CPU copy per chunk)
            outputs = binding.get_outputs()
            if not outputs:
                return None

            chunk_result = outputs[0].numpy()
            all_results.append(chunk_result.astype(np.float32))

            # Free GPU tensors before next chunk
            del input_ids_gpu, attention_mask_gpu, binding, outputs
            if token_type_ids_gpu is not None:
                del token_type_ids_gpu

        if not all_results:
            return None
        return np.concatenate(all_results, axis=0) if len(all_results) > 1 else all_results[0]

    except Exception as e:
        log.debug("IOBinding inference failed: %s", e)
        return None


def _get_onnx_batch_size() -> int:
    """Get optimal ONNX batch size based on GPU VRAM.

    Self-attention memory is O(batch × L²). At seq_len=1024:
    - batch=32: 1.6 GB attention scores (32×12×1024×1024×4B) → OOM with 3GB arena cap
    - batch=8:  400 MB attention scores → safe with 3GB arena + 800MB model weights
    Override via OPENCODE_ONNX_BATCH_SIZE env var for manual tuning.

    Sizes:
    - Low-memory mode: 4
    - No GPU: 8 (CPU-safe)
    - <8 GB VRAM: 6
    - 8–14 GB VRAM: 8
    - ≥14 GB VRAM: 8 (Blackwell/RTX 5080: 3GB arena cap leaves ~2.2GB for workspace;
      batch=32 needs 1.6GB attention scores → OOM under concurrent query+index load)
    """
    env = os.environ.get("OPENCODE_ONNX_BATCH_SIZE", "").strip()
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass

    if _LOW_MEMORY_MODE:
        return 4

    if not is_gpu_available():
        return 8

    vram_mb = _get_gpu_vram_mb()
    if vram_mb is None:
        log.info("Could not detect GPU VRAM, using default batch_size=8")
        return 8

    vram_gb = vram_mb / 1024

    # ≥8 GB: 8 is safe at seq_len≤1024 within the 4GB arena cap.
    # batch=32 requires 1.6GB self-attention scores → OOM under concurrent ops.
    batch_size = 6 if vram_gb < 8 else 8

    log.info("Auto-configured ONNX batch_size=%d for %.1fGB VRAM", batch_size, vram_gb)
    return batch_size


# Cached batch size (computed on first use)
_cached_onnx_batch_size: int | None = None


def get_onnx_batch_size() -> int:
    """Get ONNX batch size (cached for performance)."""
    global _cached_onnx_batch_size
    if _cached_onnx_batch_size is None:
        _cached_onnx_batch_size = _get_onnx_batch_size()
    return _cached_onnx_batch_size


_MAX_TOKENS = 1024  # Tokenizer truncation limit (safety net for ONNX memory)

# GPU throughput constants
EMBED_PASSAGES_MAX_TEXTS = 256
EMBED_PASSAGES_MAX_BYTES = 24 * 1024 * 1024  # 24MB

# ---------------------------------------------------------------------------
# Blackwell ONNX session reset
# ORT 1.26 + SM 12.0: CUDA workspace/free-list corrupts after ~50 forward passes
# → SIGSEGV inside the ONNX kernel. Periodic session recreation clears all state.
# ---------------------------------------------------------------------------
_embed_batch_count: int = 0
_embed_batch_count_lock = threading.Lock()
# Default 25 batches ≈ 3–5 min; override with OPENCODE_BLACKWELL_RESET_EVERY=N
_BLACKWELL_RESET_EVERY: int = int(os.environ.get("OPENCODE_BLACKWELL_RESET_EVERY", "25"))


def _maybe_reset_blackwell_session(model: str) -> None:
    """For Blackwell GPUs: reset ONNX session every N embed calls to prevent SIGSEGV."""
    global _embed_batch_count
    caps = _get_gpu_capabilities()
    if caps.get("architecture") != "blackwell":
        return
    with _embed_batch_count_lock:
        _embed_batch_count += 1
        do_reset = _embed_batch_count >= _BLACKWELL_RESET_EVERY
        if do_reset:
            _embed_batch_count = 0
    if not do_reset:
        return
    # Reset: evict the cached session so _embedder() recreates it on next call.
    # _embedder() already handles del + gc.collect() + CUDA sync.
    global _cached_embedder, _cached_embedder_model
    with _embedder_lock:
        if _cached_embedder is not None and _cached_embedder_model == model:
            old = _cached_embedder
            _cached_embedder = None
            _cached_embedder_model = None
            del old
            gc.collect()
            gc.collect()
            _cuda_sync_and_empty_cache()
            log.info(
                "Blackwell ONNX session reset after %d batches (workspace leak prevention)",
                _BLACKWELL_RESET_EVERY,
            )


def _try_embed_iobinding(embedder, texts, provider, dimensions, prefix="passage"):
    """Try IOBinding-based embedding. Returns (matrix, success_bool)."""
    session = embedder.model.model if hasattr(embedder.model, 'model') else None
    tokenizer = embedder.model.tokenizer if hasattr(embedder.model, 'tokenizer') else None
    if session is None or tokenizer is None:
        return None, False

    device = _device_for_provider(provider)
    all_items = []
    use_iobinding = True

    for start in range(0, len(texts), _EMBED_SUB_BATCH):
        batch = texts[start : start + _EMBED_SUB_BATCH]
        prefixed = [f"{prefix}: {t}" for t in batch]
        batch_result = _embed_batch_iobinding(
            session, tokenizer, prefixed, get_onnx_batch_size(), device
        )
        if batch_result is not None:
            if batch_result.ndim == 3:
                batch_result = np.mean(batch_result, axis=1)
            all_items.append(batch_result.astype(np.float32))
        else:
            use_iobinding = False
            break

    if not (use_iobinding and all_items):
        return None, False

    mat = np.concatenate(all_items, axis=0) if len(all_items) > 1 else all_items[0]
    mat = _resize_matrix(mat, dimensions)
    return mat, True


def embed_passages(
    texts: list[str],
    *,
    model: str,
    dimensions: int,
    _return_numpy: bool = False,
) -> list[list[float]] | np.ndarray:
    """Embed passage texts on GPU.

    When ``_return_numpy=True`` the raw normalized numpy matrix is returned
    (shape [N, dims], dtype float32) instead of a Python list of lists.
    Callers that slice the result and pass vectors straight to storage should
    use this flag to avoid the O(N·dims) Python float object allocation that
    ``mat.tolist()`` incurs.
    """
    if not texts:
        return np.empty((0, dimensions), dtype=np.float32) if _return_numpy else []

    touch_inference_time()
    import time

    t_start = time.perf_counter()
    # Blackwell: reset session every N batches before acquiring a new one.
    _maybe_reset_blackwell_session(model)
    embedder = _embedder(model)

    provider = get_active_provider()
    is_gpu = provider in _GPU_PROVIDERS
    if is_gpu:
        _increment_gpu_ops()
    else:
        # CPU fallback is forbidden — this should never be reached because
        # assert_gpu_available() crashes the daemon at startup. If somehow
        # the provider changed at runtime, fail hard immediately.
        _increment_cpu_ops()
        raise GPUNotAvailableError(
            f"[GPU-REQUIRED] embed_passages reached CPU provider '{provider}'. "
            "CPU inference is forbidden. GPU driver/ORT configuration error."
        )

    total_chars = sum(len(t) for t in texts)
    avg_chars = total_chars // len(texts) if texts else 0

    # IOBinding path: tensors stay on GPU; single transfer per chunk
    if is_gpu and _io_binding_confirmed:
        t_embed_start = time.perf_counter()
        with _GPU_INFER_LOCK:
            mat, ok = _try_embed_iobinding(embedder, texts, provider, dimensions, prefix="passage")
        t_embed_done = time.perf_counter()
        if ok:
            if mat.ndim == 1:
                mat = mat.reshape(1, -1)
            mat = _normalize_embeddings(mat)

            t_end = time.perf_counter()
            if log.isEnabledFor(logging.INFO):
                log.info(
                    "embed[%s+IOBinding]: %d texts (%d avg chars), onnx=%.0fms, total=%.0fms",
                    provider.upper(),
                    len(texts),
                    avg_chars,
                    (t_embed_done - t_embed_start) * 1000,
                    (t_end - t_start) * 1000,
                )
            if _return_numpy:
                return mat
            out = mat.tolist()
            del mat
            return out
        else:
            # IOBinding failed on the real model — disable permanently so we
            # don't retry on every subsequent batch (avoids wasted setup overhead).
            _set_io_binding_active(False)
            log.info("embed[%s] IOBinding failed on real model; disabling for session", provider.upper())

    # Standard path (fallback or when IOBinding unavailable)
    all_items: list = []
    t_embed_total = 0.0
    for start in range(0, len(texts), _EMBED_SUB_BATCH):
        # Priority gate: yield GPU to interactive (search) ops between sub-batches.
        if start > 0:
            yield_to_interactive()
        batch = texts[start : start + _EMBED_SUB_BATCH]
        prefixed = [f"passage: {t}" for t in batch]
        t_embed_start = time.perf_counter()
        with _GPU_INFER_LOCK:
            items = embedder.embed(prefixed, batch_size=get_onnx_batch_size())
        if _HAS_NUMPY:
            batch_arr = np.array(list(items), dtype=np.float32)
            all_items.append(batch_arr)
            del items, batch_arr
        else:
            all_items.extend(list(items))
        t_embed_done = time.perf_counter()
        t_embed_total += t_embed_done - t_embed_start

    if _HAS_NUMPY and all_items:
        mat = np.concatenate(all_items, axis=0) if len(all_items) > 1 else all_items[0]
        del all_items
        if mat.ndim == 1:
            mat = mat.reshape(1, -1)
        if dimensions > 0:
            mat = _resize_matrix(mat, dimensions)
        mat = _normalize_embeddings(mat)


        t_postprocess = time.perf_counter()
        t_end = time.perf_counter()
        if log.isEnabledFor(logging.INFO):
            t_post_ms = (t_postprocess - t_start - t_embed_total) * 1000
            log.info(
                "embed[%s]: %d texts (%d avg chars), onnx=%.0fms, post=%.1fms, total=%.0fms",
                provider.upper(),
                len(texts),
                avg_chars,
                t_embed_total * 1000,
                t_post_ms,
                (t_end - t_start) * 1000,
            )
        if _return_numpy:
            return mat
        out = mat.tolist()
        del mat
        return out
    else:
        out = []
        for item in all_items:
            vec_list = item.tolist() if hasattr(item, 'tolist') else item
            out.append(_normalize(_resize([float(x) for x in vec_list], dimensions)))
        return out


def warmup_query_models(model: str | None = None, rerank_model: str | None = None) -> None:
    """Load and pin the query embedder + reranker at startup so search is never cold.

    Called once during MCP/daemon startup BEFORE watchers and the KB sweep arm.
    This ensures the small query sessions claim VRAM first.  Idempotent; GPU-only
    (raises on CPU fallback, never degrades silently).
    """
    from opencode_search.config import DEFAULT_EMBED_MODEL, DEFAULT_RERANK_MODEL
    embed_model = model or DEFAULT_EMBED_MODEL
    rank_model = rerank_model or DEFAULT_RERANK_MODEL
    log.info("warmup_query_models: loading query embedder (%s) + reranker (%s)", embed_model, rank_model)
    _embedder(embed_model, role="query")
    _reranker(rank_model)
    log.info("warmup_query_models: query models pinned and resident")


def embed_query(text: str, *, model: str, dimensions: int) -> list[float]:
    if not text:
        return []

    import time

    t_start = time.perf_counter()
    embedder = _embedder(model, role="query")

    # Track GPU vs CPU operations
    provider = get_active_provider()
    is_gpu = provider in _GPU_PROVIDERS
    if is_gpu:
        _increment_gpu_ops()
    else:
        _increment_cpu_ops()

    _interactive_start()
    try:
        with _GPU_INFER_LOCK:
            items = list(embedder.embed([f"query: {text}"], batch_size=get_onnx_batch_size()))
    finally:
        _interactive_done()
    t_end = time.perf_counter()

    if not items:
        return []

    # Hot path optimization: keep as numpy array, use vectorized ops
    vec_np = _resize_np(items[0], dimensions)
    vec_np = _normalize_np(vec_np)
    result = (
        vec_np.tolist()
        if _HAS_NUMPY
        else _normalize(_resize([float(x) for x in items[0].tolist()], dimensions))
    )

    if log.isEnabledFor(logging.INFO):
        log.info(
            "embed_query[%s]: %d chars, %.0fms",
            provider.upper(),
            len(text),
            (t_end - t_start) * 1000,
        )
    return result


def embed_passages_f32_bytes(
    texts: list[str], *, model: str, dimensions: int
) -> tuple[bytes, int, int]:
    """Embed passages and return little-endian float32 bytes.

    Avoids Python list materialization for vectors.
    Returns: (vectors_f32_bytes, dimensions, count)
    """
    if not texts:
        return b"", dimensions, 0

    import sys
    import time

    embedder = _embedder(model)

    provider = get_active_provider()
    is_gpu = provider in _GPU_PROVIDERS
    if is_gpu:
        _increment_gpu_ops()
    else:
        _increment_cpu_ops()

    t_start = time.perf_counter()
    dim_out = dimensions
    # Try IOBinding path if GPU available
    if is_gpu and _io_binding_confirmed:
        t_embed_start = time.perf_counter()
        with _GPU_INFER_LOCK:
            mat, ok = _try_embed_iobinding(embedder, texts, provider, dimensions, prefix="passage")
        t_embed_done = time.perf_counter()
        if ok:
            if mat.ndim == 1:
                mat = mat.reshape(1, -1)

            dim_out = int(mat.shape[1]) if mat.size else dimensions
            mat = _normalize_embeddings(mat)
            mat = np.asarray(mat, dtype="<f4")
            out_bytes = mat.tobytes()
            del mat
            gc.collect()

            t_end = time.perf_counter()
            if log.isEnabledFor(logging.INFO):
                log.info(
                    "embed_f32[%s+IOBinding]: %d texts, onnx=%.0fms, total=%.0fms",
                    provider.upper(),
                    len(texts),
                    (t_embed_done - t_embed_start) * 1000,
                    (t_end - t_start) * 1000,
                )
            return out_bytes, dim_out, len(texts)

    # Standard path (fallback)
    all_items: list = []
    t_embed_total = 0.0
    for start in range(0, len(texts), _EMBED_SUB_BATCH):
        # Priority gate: yield GPU to interactive (search) ops between sub-batches.
        if start > 0:
            yield_to_interactive()
        batch = texts[start : start + _EMBED_SUB_BATCH]
        prefixed = [f"passage: {t}" for t in batch]

        t_embed_start = time.perf_counter()
        with _GPU_INFER_LOCK:
            items = embedder.embed(prefixed, batch_size=get_onnx_batch_size())
        # Convert to numpy immediately (single copy)
        if _HAS_NUMPY:
            batch_arr = np.array(list(items), dtype=np.float32)
            all_items.append(batch_arr)
            del items, batch_arr
        else:
            all_items.extend(list(items))
        t_embed_done = time.perf_counter()
        t_embed_total += t_embed_done - t_embed_start
        # Aggressive GC after each batch
        gc.collect()

    # Single-pass vectorized normalize + serialize
    t_post_start = time.perf_counter()
    if _HAS_NUMPY and all_items:
        # Concatenate once instead of repeated extend
        mat = np.concatenate(all_items, axis=0) if len(all_items) > 1 else all_items[0]
        del all_items
        gc.collect()

        if getattr(mat, "ndim", 0) == 1:
            mat = mat.reshape(1, -1)

        if dimensions > 0:
            mat = _resize_matrix(mat, dimensions)

        dim_out = int(mat.shape[1]) if mat.size else dimensions
        mat = _normalize_embeddings(mat)
        mat = np.asarray(mat, dtype="<f4")
        out_bytes = mat.tobytes()
        del mat
    elif all_items:
        import array

        buf = array.array("f")
        for item in all_items:
            vec_list = item.tolist() if hasattr(item, 'tolist') else item
            vec = _normalize(_resize([float(x) for x in vec_list], dimensions))
            buf.fromlist(vec)  # type: ignore[arg-type]
        if sys.byteorder != "little":
            buf.byteswap()
        dim_out = dimensions
        out_bytes = buf.tobytes()
    else:
        out_bytes = b""

    t_post_total = time.perf_counter() - t_post_start
    # Final cleanup
    gc.collect()

    t_end = time.perf_counter()
    if log.isEnabledFor(logging.INFO):
        log.info(
            "embed_f32[%s]: %d texts, onnx=%.0fms, post=%.1fms, total=%.0fms",
            provider.upper(),
            len(texts),
            t_embed_total * 1000,
            t_post_total * 1000,
            (t_end - t_start) * 1000,
        )
    return out_bytes, dim_out, len(texts)


def embed_query_f32_bytes(text: str, *, model: str, dimensions: int) -> tuple[bytes, int]:
    """Embed a query and return little-endian float32 bytes."""
    if not text:
        return b"", dimensions

    import sys
    import time

    embedder = _embedder(model, role="query")

    provider = get_active_provider()
    is_gpu = provider in _GPU_PROVIDERS
    if is_gpu:
        _increment_gpu_ops()
    else:
        _increment_cpu_ops()

    t_start = time.perf_counter()
    _interactive_start()
    try:
        items = list(embedder.embed([f"query: {text}"], batch_size=get_onnx_batch_size()))
    finally:
        _interactive_done()
    if not items:
        return b"", dimensions

    # Hot path optimization: check numpy once at module load, not here
    if _HAS_NUMPY:
        vec = np.asarray(items[0], dtype=np.float32)
        if dimensions > 0:
            if vec.shape[0] > dimensions:
                vec = vec[:dimensions]
            elif vec.shape[0] < dimensions:
                tmp = np.zeros((dimensions,), dtype=np.float32)
                tmp[: vec.shape[0]] = vec
                vec = tmp

        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec = vec / norm
        vec = np.asarray(vec, dtype="<f4")
        t_end = time.perf_counter()
        if log.isEnabledFor(logging.INFO):
            log.info(
                "embed_query_f32[%s]: %d chars, %.0fms",
                provider.upper(),
                len(text),
                (t_end - t_start) * 1000,
            )
        return vec.tobytes(), int(vec.shape[0])
    else:
        # Fallback when numpy not available (should rarely happen)
        import array

        # Hot path optimization: use vectorized ops when available
        vec_np = _resize_np(items[0], dimensions)
        vec_np = _normalize_np(vec_np)
        vec = (
            vec_np.tolist()
            if _HAS_NUMPY
            else _normalize(_resize([float(x) for x in items[0].tolist()], dimensions))
        )
        buf = array.array("f")
        buf.fromlist(vec)  # type: ignore[arg-type]
        if sys.byteorder != "little":
            buf.byteswap()
        t_end = time.perf_counter()
        if log.isEnabledFor(logging.INFO):
            log.info(
                "embed_query_f32[%s]: %d chars, %.0fms",
                provider.upper(),
                len(text),
                (t_end - t_start) * 1000,
            )
        return buf.tobytes(), len(vec)


# R5: Per-model temperature defaults for sigmoid calibration.
# Override via OPENCODE_RERANK_TEMPERATURE env var.
RERANK_TEMPERATURE: dict[str, float] = {
    "Xenova/ms-marco-MiniLM-L-6-v2":              1.0,
    "jinaai/jina-reranker-v1-turbo-en":            1.0,
    "jinaai/jina-reranker-v2-base-multilingual":   1.0,
}


def _calibrate_scores(logits, temperature: float = 1.0) -> np.ndarray:
    """Convert raw cross-encoder logits to [0,1] via sigmoid.

    Sigmoid preserves absolute meaning: docs scoring [5.01..5.05] all map to
    ~0.993, correctly showing they are all highly relevant. Min-max would spread
    them across [0.0, 1.0], creating a false ranking signal from noise.
    """
    arr = np.asarray(logits, dtype=np.float32)
    return 1.0 / (1.0 + np.exp(-arr / temperature))


def _get_rerank_batch_size() -> int:
    """VRAM-scaled rerank batch size (conservative to share VRAM with embed workers)."""
    if _LOW_MEMORY_MODE:
        return 8
    vram_mb = _get_gpu_vram_mb()
    if vram_mb is None:
        return 16
    vram_gb = vram_mb / 1024
    if vram_gb < 8:
        return 8
    if vram_gb < 15:
        # Same threshold fix as embed batch_size: 15.92GB RTX 5080 Laptop → 32
        return 16
    return 32  # >=15GB (RTX 5080 Laptop, RTX 3090, etc.)


def _rerank_batched(reranker, query: str, docs: list[str], batch_size: int) -> list[float]:
    """Run reranker inference in explicit batches of `batch_size` pairs."""
    all_scores: list[float] = []
    for i in range(0, len(docs), batch_size):
        batch = docs[i : i + batch_size]
        all_scores.extend(reranker.rerank(query, batch))
    return all_scores


def _rerank_iobinding(session, tokenizer, query: str, docs: list[str], batch_size: int, device: str = "cuda") -> list[float] | None:
    """Run cross-encoder reranking with IOBinding (single GPU→CPU copy per batch).

    Returns list of raw logit scores or None if IOBinding fails.
    """
    try:
        import onnxruntime as ort

        all_scores: list[float] = []
        pairs = [[query, doc] for doc in docs]
        for i in range(0, len(pairs), batch_size):
            batch = pairs[i : i + batch_size]
            encoded = tokenizer.encode_batch(batch)
            input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
            attn_mask  = np.array([e.attention_mask for e in encoded], dtype=np.int64)

            binding = session.io_binding()
            ids_gpu  = ort.OrtValue.ortvalue_from_numpy(input_ids, device)
            mask_gpu = ort.OrtValue.ortvalue_from_numpy(attn_mask, device)
            binding.bind_ortvalue_input("input_ids", ids_gpu)
            binding.bind_ortvalue_input("attention_mask", mask_gpu)

            # Handle optional token_type_ids (some models include it)
            if "token_type_ids" in _rerank_input_names:
                token_type_ids = np.zeros_like(input_ids)
                tti_gpu = ort.OrtValue.ortvalue_from_numpy(token_type_ids, device)
                binding.bind_ortvalue_input("token_type_ids", tti_gpu)

            for out in session.get_outputs():
                binding.bind_output(out.name, device)

            session.run_with_iobinding(binding)
            logits = binding.get_outputs()[0].numpy()
            if logits.ndim > 1:
                logits = logits.squeeze(-1)
            all_scores.extend(logits.tolist())
            del ids_gpu, mask_gpu, binding
        return all_scores
    except Exception as e:
        log.debug("reranker IOBinding failed: %s", e)
        return None


def rerank(query: str, docs: list[str], *, model: str, top_k: int) -> list[tuple[int, float]]:
    if not docs or top_k <= 0:
        return []

    touch_inference_time()
    import time

    t_start = time.perf_counter()
    reranker = _reranker(model)

    provider = get_active_provider()
    is_gpu = provider in _GPU_PROVIDERS
    if is_gpu:
        _increment_gpu_ops()
    else:
        _increment_cpu_ops()

    # Rerank is always interactive (query path) — mark it high-priority so
    # background passage-embed batches yield at their next sub-batch boundary.
    _interactive_start()
    try:
        return _rerank_inner(reranker, provider, is_gpu, query, docs, model, top_k, t_start)
    finally:
        _interactive_done()


def _rerank_inner(
    reranker: object,
    provider: str,
    is_gpu: bool,
    query: str,
    docs: list[str],
    model: str,
    top_k: int,
    t_start: float,
) -> list[tuple[int, float]]:
    import time

    batch_size = _get_rerank_batch_size()
    scores: list[float] | None = None

    # R4: Try IOBinding fast path (single GPU→CPU copy per batch)
    if is_gpu and _rerank_iobinding_confirmed:
        try:
            session = None
            tokenizer = None
            if hasattr(reranker, "model"):
                inner = reranker.model
                if hasattr(inner, "model"):
                    session = inner.model
                if hasattr(inner, "tokenizer"):
                    tokenizer = inner.tokenizer
            if session is not None and tokenizer is not None:
                device = _device_for_provider(provider)
                with _GPU_INFER_LOCK:
                    scores = _rerank_iobinding(session, tokenizer, query, docs, batch_size, device)
        except Exception as e:
            log.debug("reranker IOBinding path failed, falling back: %s", e)
            scores = None

    # R3: Standard batched path (fallback)
    if scores is None:
        with _GPU_INFER_LOCK:
            scores = _rerank_batched(reranker, query, docs, batch_size)

    t_end = time.perf_counter()

    if not scores:
        return []

    # R5: Sigmoid calibration — preserves absolute relevance meaning.
    # Opt-in to legacy min-max via OPENCODE_RERANK_NORMALIZE=minmax.
    normalize_mode = os.environ.get("OPENCODE_RERANK_NORMALIZE", "sigmoid").lower()
    temperature = float(os.environ.get(
        "OPENCODE_RERANK_TEMPERATURE",
        str(RERANK_TEMPERATURE.get(model, 1.0)),
    ))

    if normalize_mode == "minmax":
        scores_arr = np.array(scores, dtype=np.float32)
        lo, hi = float(scores_arr.min()), float(scores_arr.max())
        if hi == lo:
            normed = np.full(len(scores), 0.5, dtype=np.float32)
        else:
            normed = (scores_arr - lo) / (hi - lo)
    else:
        normed = _calibrate_scores(scores, temperature)

    order = np.argsort(normed)[::-1][:top_k].tolist()
    normed_list = normed.tolist()

    if log.isEnabledFor(logging.INFO):
        log.info(
            "rerank[%s%s]: %d docs -> top %d, batch=%d, %.0fms",
            provider.upper(),
            "+IOB" if is_gpu and _rerank_iobinding_confirmed else "",
            len(docs),
            min(top_k, len(docs)),
            batch_size,
            (t_end - t_start) * 1000,
        )
    return [(i, float(normed_list[i])) for i in order]


# ---------------------------------------------------------------------------
# Internal helpers expected by tests
# ---------------------------------------------------------------------------

# CPU count available to the process (used by tests and thread scaling)
_cpus: int = os.cpu_count() or 2

# RAM detection (MB) — used for hardware profiling
try:
    import psutil as _psutil
    _ram_mb: int = int(_psutil.virtual_memory().total / 1024 / 1024)
except Exception:
    _ram_mb = 8192  # default 8 GB

# Hardware profile flags
_LOW_END: bool = _cpus <= 4 or _ram_mb <= 8192
_HIGH_END: bool = _cpus >= 16 and _ram_mb >= 32768

# Sub-batch size for chunked ONNX inference — hardware adaptive.
# This controls how many texts are passed to embedder.embed() at once.
# With ONNX batch_size=128 and _EMBED_SUB_BATCH=512: ceil(512/128)=4 kernel
# calls per embed_passages invocation (vs 16 when batch_size=32+sub_batch=256).
_EMBED_SUB_BATCH: int
if os.environ.get("OPENCODE_EMBED_SUB_BATCH"):
    _EMBED_SUB_BATCH = int(os.environ["OPENCODE_EMBED_SUB_BATCH"])
elif os.environ.get("OPENCODE_EMBED_LOW_MEMORY", "").strip() in ("1", "true", "yes"):
    _EMBED_SUB_BATCH = 8
elif _LOW_END:
    _EMBED_SUB_BATCH = 64
elif _HIGH_END:
    _EMBED_SUB_BATCH = 256
else:
    _EMBED_SUB_BATCH = 256


def get_embed_workers_gpu(vram_mb: int | None = None) -> int:
    """VRAM-scaled embed worker count: min(6, max(2, (vram - 1024) // 600))."""
    if vram_mb is None:
        vram_mb = _get_gpu_vram_mb() or 0
    if vram_mb <= 0:
        return 2
    return min(6, max(2, (vram_mb - 1024) // 600))


def get_embed_batch_chunks() -> int:
    """Return optimal GPU batcher chunk count based on available VRAM.

    Larger batches amortize CUDA kernel launch overhead and keep GPU utilization
    high. Each chunk is ~200–500 chars of text; 512 chunks * 768-dim vectors ≈
    1.5 MB VRAM — well within budget even on 4 GB cards when considering only
    vector data (not ONNX model weight memory).

    Override via OPENCODE_EMBED_BATCH_CHUNKS env var.
    """
    env = os.environ.get("OPENCODE_EMBED_BATCH_CHUNKS", "").strip()
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass

    vram_mb = _get_gpu_vram_mb()
    if vram_mb is None or vram_mb <= 0:
        return 64  # no GPU detected, keep small

    vram_gb = vram_mb / 1024
    if vram_gb >= 14:
        # RTX 5080 16 GB: cap at 128. Blackwell (SM 12.0) ONNX-CUDA workspace
        # does not free between calls; 512-chunk batches of long-text files
        # (~1900 chars) exhaust 4.6 GB free VRAM after 2–3 flush cycles → SIGSEGV.
        # 128 keeps peak activation ≤1 GB, avoids workspace fragmentation.
        return 128
    elif vram_gb >= 8:
        return 128   # conservative for non-Blackwell 8-16 GB cards
    elif vram_gb >= 4:
        return 64    # GTX 1650 4GB
    else:
        return 32    # low VRAM — keep conservative


def _get_test_model_path() -> str:
    """Return path to a cached ONNX model used for provider testing.

    Uses the first available model from the fastembed/HuggingFace cache.
    Falls back to a well-known cached model path.
    """
    import glob as _glob

    # Look for any cached .onnx model (fastembed downloads these at warmup)
    cache_dirs = [
        os.path.expanduser("~/.cache/huggingface/hub"),
        os.path.expanduser("~/.cache/fastembed"),
    ]
    for base in cache_dirs:
        matches = _glob.glob(f"{base}/**/*.onnx", recursive=True)
        if matches:
            # Prefer smaller models for faster provider testing
            matches.sort(key=os.path.getsize)
            return matches[0]

    raise FileNotFoundError(
        "No cached ONNX model found for provider testing. "
        "Run the server once to download models so ONNX provider testing can proceed."
    )


def _test_provider(provider: str) -> bool:
    """Test whether an ONNX provider works with a minimal model.

    Returns True if the provider can run inference, False otherwise.
    Cleans up the ONNX session in a finally block.
    """
    import numpy as np

    session = None
    try:
        import onnxruntime as ort

        try:
            model_path = _get_test_model_path()
        except FileNotFoundError as exc:
            log.info("%s provider pre-test deferred: %s", provider, exc)
            return True

        so = ort.SessionOptions()
        so.log_severity_level = 3

        provider_list = [(provider, {})] if provider in _GPU_PROVIDERS else [provider]

        session = ort.InferenceSession(model_path, so, providers=provider_list)
        active = session.get_providers()
        if provider not in active:
            return False

        feeds = {}
        for inp in session.get_inputs():
            shape = [1 if not isinstance(dim, int) or dim <= 0 else dim for dim in inp.shape]
            if not shape:
                shape = [1]
            if "int64" in inp.type:
                feeds[inp.name] = np.ones(shape, dtype=np.int64)
            elif "int32" in inp.type:
                feeds[inp.name] = np.ones(shape, dtype=np.int32)
            elif "bool" in inp.type:
                feeds[inp.name] = np.ones(shape, dtype=bool)
            else:
                feeds[inp.name] = np.ones(shape, dtype=np.float32)

        result = session.run(None, feeds)
        return bool(result)

    except Exception as e:
        log.debug("%s provider test failed: %s", provider, e)
        return False
    finally:
        if session is not None:
            del session
            gc.collect()


def _verify_onnx_session_provider(model_obj, name: str) -> None:
    """Verify the ONNX session is running on GPU, raise if not.

    Also re-confirms IOBinding on the actual model session.

    Bug fix: _io_binding_confirmed was only set during _test_provider() on the
    tiny identity test model. If the real embedding model's ONNX graph uses
    different input names or shapes, IOBinding could silently fail and the flag
    would remain False, blocking the IOBinding fast path forever.
    """
    try:
        session = None
        if hasattr(model_obj, "model"):
            inner = model_obj.model
            if hasattr(inner, "model"):
                session = inner.model
            elif hasattr(inner, "session"):
                session = inner.session
        if session is None:
            raise RuntimeError(
                f"[{name}] GPU ENFORCEMENT FAILED: Could not access ONNX session."
            )
        active = session.get_providers()
        log.info("[%s] ONNX session active providers: %s", name, active)
        using_gpu = any(p in _GPU_PROVIDERS for p in active)
        if using_gpu:
            gpu_name = next(p for p in active if p in _GPU_PROVIDERS)
            log.info("[%s] GPU ACTIVE: Using %s for inference", name, gpu_name)

            # Bug fix: re-confirm IOBinding on the actual model session.
            # The test-model confirmation in _test_provider() used input name "X".
            # Real embedding models use "input_ids"/"attention_mask"; if the
            # session doesn't support io_binding() the flag stays False and the
            # fast path is never attempted even when it would work.
            if not _io_binding_confirmed:
                try:
                    _probe = session.io_binding()  # raises if not supported
                    # Bind a dummy output just to confirm the API works
                    for out in session.get_outputs():
                        _probe.bind_output(out.name, _device_for_provider(gpu_name))
                        break  # one output is enough to confirm
                    _set_io_binding_active(True)
                    log.info("[%s] IOBinding confirmed on real model session", name)
                except Exception as e:
                    log.info("[%s] IOBinding not supported by model session: %s", name, e)
                    _set_io_binding_active(False)
        else:
            raise RuntimeError(
                f"[{name}] GPU ENFORCEMENT FAILED: ONNX session fell back to CPU. "
                f"Active providers: {active}."
            )
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"[{name}] GPU ENFORCEMENT FAILED: {e}") from e
