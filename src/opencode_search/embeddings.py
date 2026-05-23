"""Local embedding + rerank backend (free).

Implements embeddings and reranking using open-source models via FastEmbed.

Notes:
- Models are downloaded and cached locally by FastEmbed on first use.
- No provider API keys are required.
- Models are unloaded when idle to free ~1-2 GB of RAM per project.
"""

from __future__ import annotations

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

# Tier -> model mappings (must match Rust's cli.rs models_for_tier)
# Budget tier is the default when model parameter is empty
TIER_MODELS = {
    "premium": {
        "embed": "jinaai/jina-embeddings-v2-base-code",
        "rerank": "jinaai/jina-reranker-v2-base-multilingual",
    },
    "balanced": {
        "embed": "jinaai/jina-embeddings-v2-base-en",
        "rerank": "jinaai/jina-reranker-v1-turbo-en",
    },
    "budget": {
        "embed": "jinaai/jina-embeddings-v2-small-en",
        "rerank": "Xenova/ms-marco-MiniLM-L-6-v2",
    },
}

# Default models (budget tier - used when model parameter is empty)
DEFAULT_EMBED_MODEL = TIER_MODELS["budget"]["embed"]
DEFAULT_RERANK_MODEL = TIER_MODELS["budget"]["rerank"]

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
_GPU_NORMALIZE_MODE = os.environ.get("OPENCODE_GPU_NORMALIZE", "auto").lower()

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
                import re

                match = re.search(
                    r"(Apple M\d+\s*(?:Pro|Max|Ultra|Nano)?)", out.stdout, re.IGNORECASE
                )
                if match:
                    result["vendor"] = "apple"
                    result["gpu_name"] = match.group(1).strip()
                    result["supports_fp16"] = True  # All Apple Silicon supports FP16 via Metal
                    m = re.search(r"M(\d+)", result["gpu_name"], re.IGNORECASE)
                    # M3+ has hardware ray tracing; treat all M-series as capable
                    result["has_tensor_cores"] = bool(m and int(m.group(1)) >= 3)
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
                    import re

                    result["vendor"] = "qualcomm"
                    for line in out.stdout.strip().split("\n")[1:]:
                        low = line.lower()
                        if "qualcomm" in low or "adreno" in low:
                            result["gpu_name"] = line.strip()
                            break
                    name_low = (result["gpu_name"] or "").lower()
                    # Adreno 690+ supports FP16 tensor ops
                    m = re.search(r"adreno\s*(\d+)", name_low)
                    if m and int(m.group(1)) >= 690:
                        result["supports_fp16"] = True
                    elif "adreno 7" in name_low:
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
    Falls back to CPU if GPU fails or CuPy unavailable.
    """
    cp_mod = _get_cupy()
    if cp_mod is None:
        return _normalize_embeddings_cpu(mat)

    try:
        # Transfer to GPU, normalize, transfer back
        mat_gpu = cp_mod.asarray(mat, dtype=cp_mod.float32)
        norms = cp_mod.linalg.norm(mat_gpu, axis=1, keepdims=True)
        cp_mod.divide(mat_gpu, norms, out=mat_gpu, where=norms > 0)
        result = cp_mod.asnumpy(mat_gpu)

        # Clean up GPU memory
        del mat_gpu, norms
        return result
    except Exception as e:
        # Fallback to CPU if GPU fails (OOM, driver issues, etc.)
        log.debug(f"GPU normalization failed, falling back to CPU: {e}")
        return _normalize_embeddings_cpu(mat)


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


# Manual model cache (replaces @lru_cache to support explicit unloading).
# When idle in watch mode, models are unloaded to free ~1-2 GB of RAM.
# They reload on-demand when the next file change is detected (~2-5s cost).
# Thread-safe access via locks to prevent race conditions.
_embedder_lock = threading.Lock()
_cached_embedder: object | None = None
_cached_embedder_model: str | None = None

# Idle inference tracking — used by the daemon cleanup loop to unload models
# after a configurable period of no embed/rerank calls.
import time as _time_module  # noqa: E402 — placed here to keep with related state

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


# Monkey-patch FastEmbed to accept additional ORT SessionOptions not in default
# EXPOSED_SESSION_OPTIONS. Reduces CPU memory when inference runs on GPU.
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
                try:
                    session_options.add_session_config_entry("session.disable_prepacking", "1")
                except Exception:
                    pass
        _OnnxModel.add_extra_session_options = _patched_add_extra
        del _patched_add_extra
except ImportError:
    pass


_ONNX_LOG_SEVERITY_LEVEL = int(os.environ.get("OPENCODE_ONNX_LOG_SEVERITY", "3"))


def _embedder(model: str):
    """Return (and cache) a FastEmbed TextEmbedding model loaded with GPU providers."""
    global _cached_embedder, _cached_embedder_model
    if not model:
        model = DEFAULT_EMBED_MODEL
    with _embedder_lock:
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
            # Release the FastEmbed instance (which holds ONNX session + CUDA memory)
            del old
            # Multiple GC passes: first pass finds unreachable, second pass
            # collects objects with __del__ (like ONNX sessions).
            gc.collect()
            gc.collect()
            # Synchronize CUDA to flush any pending deallocations before
            # allocating VRAM for the next model load.
            _cuda_sync_and_empty_cache()

        from fastembed import TextEmbedding

        providers = _get_onnx_providers()
        log.info(
            "Loading embedding model: %s with providers: %s",
            model,
            providers or "default (CPU)",
        )

        # Bug fix: always inject provider options regardless of list length.
        # Previously guarded on len>=2 which skipped CUDA options for single-item lists.
        provider_list = _build_provider_list_with_options(providers) if providers else None

        import onnxruntime as _ort_ref
        embedder = TextEmbedding(
            model_name=model, providers=provider_list,
            enable_cpu_mem_arena=False, enable_mem_pattern=False,
            execution_mode=_ort_ref.ExecutionMode.ORT_SEQUENTIAL,
            graph_optimization_level=_ort_ref.GraphOptimizationLevel.ORT_ENABLE_EXTENDED,
            log_severity_level=_ONNX_LOG_SEVERITY_LEVEL,
        )

        # Verify which provider the ONNX session actually selected
        _verify_onnx_session_provider(embedder, "embedder")

        # Cap token length to prevent O(nu00b2) attention memory explosion
        try:
            embedder.model.tokenizer.enable_truncation(max_length=_MAX_TOKENS)
        except (AttributeError, Exception):
            pass

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

        from fastembed.rerank.cross_encoder import TextCrossEncoder

        providers = _get_onnx_providers()
        log.info(
            "Loading reranker model: %s with providers: %s",
            model,
            providers or "default (CPU)",
        )

        provider_list = _build_provider_list_with_options(providers) if providers else None

        import onnxruntime as _ort_ref
        reranker = TextCrossEncoder(
            model_name=model, providers=provider_list,
            enable_cpu_mem_arena=False, enable_mem_pattern=False,
            execution_mode=_ort_ref.ExecutionMode.ORT_SEQUENTIAL,
            graph_optimization_level=_ort_ref.GraphOptimizationLevel.ORT_ENABLE_EXTENDED,
            log_severity_level=_ONNX_LOG_SEVERITY_LEVEL,
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


def cleanup_models() -> None:
    """Release cached ONNX models to free VRAM and RAM.

    Models reload on next inference call (~2-5s cost).
    Safe to call from any thread — uses locks internally.
    """
    global _cached_embedder, _cached_embedder_model

    with _embedder_lock:
        if _cached_embedder is not None:
            old = _cached_embedder
            _cached_embedder = None
            _cached_embedder_model = None
            del old

    with _reranker_cache_lock:
        for model_name in list(_reranker_cache.keys()):
            old_r = _reranker_cache.pop(model_name, None)
            if old_r is not None:
                del old_r
        _reranker_lru.clear()

    gc.collect()
    gc.collect()  # second pass for objects with __del__
    _cuda_sync_and_empty_cache()
    log.info("cleanup_models: released cached embedder and all rerankers")


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
        # Auto-detect: check if any GPU provider is available
        try:
            import onnxruntime as _ort
            gpu_mode = any(p in _GPU_PROVIDERS for p in _ort.get_available_providers())
        except Exception:
            pass
    if low_memory or gpu_mode:
        threads = "1"  # GPU mode: CPU threads only for tokenization, 1 is enough
    elif cpus <= 4:
        threads = "1"
    elif cpus <= 8:
        threads = "2"
    elif cpus <= 16:
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

    Raises GPUNotAvailableError if no working GPU provider is found.
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
    vram_mb = caps.get("vram_mb") or _get_gpu_vram_mb()

    base: dict = {"arena_extend_strategy": "kSameAsRequested"}
    if vram_mb:
        # Reserve 80% of VRAM; leave headroom for OS and other processes
        base["gpu_mem_limit"] = str(int(vram_mb * 0.8 * 1024 * 1024))

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
                    # Blackwell (SM 12.0+): NHWC layout for better tensor core utilization
                    opts["prefer_nhwc"] = "1"
                    # Disable CUDA Graphs on Blackwell — ORT 1.25 + CUDA 13 deadlocks
                    # when mixing IOBinding with standard session.run() after graph capture.
                    # Re-enable when ORT ships Blackwell-validated CUDA Graph support.
                    del opts["enable_cuda_graph"]
                    log.info("Blackwell GPU (SM %.1f): disabled CUDA Graphs (ORT compat)", cc_float)
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

    Returns numpy array of embeddings (single GPU→CPU copy at end) or None if IOBinding fails.
    """
    try:
        import onnxruntime as ort

        # Tokenize on CPU (fast, unavoidable)
        encoded = tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        input_names = {node.name for node in session.get_inputs()}

        # Create IOBinding for GPU tensors
        binding = session.io_binding()

        # Bind inputs to GPU
        input_ids_gpu = ort.OrtValue.ortvalue_from_numpy(input_ids, device, device_id)
        attention_mask_gpu = ort.OrtValue.ortvalue_from_numpy(attention_mask, device, device_id)

        binding.bind_ortvalue_input("input_ids", input_ids_gpu)
        binding.bind_ortvalue_input("attention_mask", attention_mask_gpu)

        if "token_type_ids" in input_names:
            token_type_ids = np.array(
                [getattr(item, "type_ids", [0] * len(item.ids)) for item in encoded],
                dtype=np.int64,
            )
            token_type_ids_gpu = ort.OrtValue.ortvalue_from_numpy(
                token_type_ids, device, device_id
            )
            binding.bind_ortvalue_input("token_type_ids", token_type_ids_gpu)
        else:
            token_type_ids_gpu = None

        # Bind output to GPU (avoids CPU allocation)
        output_names = [o.name for o in session.get_outputs()]
        for name in output_names:
            binding.bind_output(name, device)

        # Run inference with IOBinding (tensors stay on GPU)
        session.run_with_iobinding(binding)

        # Extract output (single GPU→CPU copy)
        outputs = binding.get_outputs()
        if not outputs:
            return None

        # Get first output (last_hidden_state or pooled output)
        result = outputs[0].numpy()

        # Clean up GPU tensors
        del input_ids_gpu, attention_mask_gpu, binding, outputs
        if token_type_ids_gpu is not None:
            del token_type_ids_gpu
        return result

    except Exception as e:
        log.debug("IOBinding inference failed: %s", e)
        return None


def _get_onnx_batch_size() -> int:
    """Get optimal ONNX batch size based on GPU specs.

    Conservative scaling to avoid OOM with concurrent requests:
    - Low-memory mode: 4 (minimal memory usage)
    - No GPU or <4GB: 8 (CPU-optimized)
    - 4-8GB VRAM: 8
    - 8-16GB VRAM: 12
    - 16-24GB VRAM: 32
    - >24GB VRAM: 32

    Note: With concurrent workers, total GPU memory usage can be
    batch_size * workers * sequence_length. Smaller batches are more reliable.
    """
    # Check for low-memory mode override
    if _LOW_MEMORY_MODE:
        return 4

    if not is_gpu_available():
        return 8  # CPU optimal

    vram_mb = _get_gpu_vram_mb()
    if vram_mb is None:
        log.info("Could not detect GPU VRAM, using default batch_size=8")
        return 8

    vram_gb = vram_mb / 1024

    # More conservative defaults to avoid memory issues
    if vram_gb < 8:
        batch_size = 6  # Reduced from 8 for <8GB VRAM
    elif vram_gb < 16:
        batch_size = 8  # Reduced from 12
    elif vram_gb < 24:
        batch_size = 32  # Updated for >=16GB VRAM
    else:
        batch_size = 32  # Updated cap for >24GB VRAM

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


def embed_passages(texts: list[str], *, model: str, dimensions: int) -> list[list[float]]:
    if not texts:
        return []

    touch_inference_time()
    import time

    t_start = time.perf_counter()
    embedder = _embedder(model)

    # Track GPU vs CPU operations
    provider = get_active_provider()
    is_gpu = provider in _GPU_PROVIDERS
    if is_gpu:
        _increment_gpu_ops()
    else:
        _increment_cpu_ops()

    total_chars = sum(len(t) for t in texts)
    avg_chars = total_chars // len(texts) if texts else 0

    # Try IOBinding path first if GPU available and active
    if is_gpu and _io_binding_confirmed:
        t_embed_start = time.perf_counter()
        mat, ok = _try_embed_iobinding(embedder, texts, provider, dimensions, prefix="passage")
        t_embed_done = time.perf_counter()
        if ok:
            if mat.ndim == 1:
                mat = mat.reshape(1, -1)
            mat = _normalize_embeddings(mat)
            out = mat.tolist()
            del mat
            gc.collect()

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
            return out

    # Standard path (fallback or when IOBinding unavailable)
    all_items: list = []
    t_embed_total = 0.0
    for start in range(0, len(texts), _EMBED_SUB_BATCH):
        batch = texts[start : start + _EMBED_SUB_BATCH]
        prefixed = [f"passage: {t}" for t in batch]
        t_embed_start = time.perf_counter()
        # Keep as generator to avoid materializing full list in memory
        items = embedder.embed(prefixed, batch_size=get_onnx_batch_size())
        # Convert to numpy array immediately (single copy from GPU)
        if _HAS_NUMPY:
            batch_arr = np.array(list(items), dtype=np.float32)
            all_items.append(batch_arr)
            del items, batch_arr
        else:
            all_items.extend(list(items))
        t_embed_done = time.perf_counter()
        t_embed_total += t_embed_done - t_embed_start

    # Single-pass vectorized normalize across the full result matrix
    if _HAS_NUMPY and all_items:
        # Concatenate all batches once (avoid repeated extend copies)
        mat = np.concatenate(all_items, axis=0) if len(all_items) > 1 else all_items[0]
        del all_items  # Free batch list immediately

        if mat.ndim == 1:
            mat = mat.reshape(1, -1)
        if dimensions > 0:
            mat = _resize_matrix(mat, dimensions)
        mat = _normalize_embeddings(mat)
        # ONLY convert to list at final output (single .tolist() call)
        out = mat.tolist()
        del mat
    else:
        out = []
        for item in all_items:
            vec_list = item.tolist() if hasattr(item, 'tolist') else item
            out.append(_normalize(_resize([float(x) for x in vec_list], dimensions)))

    t_postprocess = time.perf_counter()
    # Final GC to clean up intermediate arrays
    gc.collect()

    t_end = time.perf_counter()
    if log.isEnabledFor(logging.INFO):
        # Bug fix: t_embed_start was a loop variable referencing only the LAST
        # batch's start time, making the "post" duration wrong for multi-batch
        # inputs. Use t_postprocess - (t_start + t_embed_total) instead.
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
    return out


def embed_query(text: str, *, model: str, dimensions: int) -> list[float]:
    if not text:
        return []

    import time

    t_start = time.perf_counter()
    embedder = _embedder(model)

    # Track GPU vs CPU operations
    provider = get_active_provider()
    is_gpu = provider in _GPU_PROVIDERS
    if is_gpu:
        _increment_gpu_ops()
    else:
        _increment_cpu_ops()

    items = list(embedder.embed([f"query: {text}"], batch_size=get_onnx_batch_size()))
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
        batch = texts[start : start + _EMBED_SUB_BATCH]
        prefixed = [f"passage: {t}" for t in batch]

        t_embed_start = time.perf_counter()
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

    embedder = _embedder(model)

    provider = get_active_provider()
    is_gpu = provider in _GPU_PROVIDERS
    if is_gpu:
        _increment_gpu_ops()
    else:
        _increment_cpu_ops()

    t_start = time.perf_counter()
    items = list(embedder.embed([f"query: {text}"], batch_size=get_onnx_batch_size()))
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
    if vram_gb < 16:
        return 16
    return 32  # RTX 5080 (16GB): 32 pairs per batch


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
                scores = _rerank_iobinding(session, tokenizer, query, docs, batch_size, device)
        except Exception as e:
            log.debug("reranker IOBinding path failed, falling back: %s", e)
            scores = None

    # R3: Standard batched path (fallback)
    if scores is None:
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

# Sub-batch size for chunked ONNX inference — hardware adaptive
_EMBED_SUB_BATCH: int
if os.environ.get("OPENCODE_EMBED_SUB_BATCH"):
    _EMBED_SUB_BATCH = int(os.environ["OPENCODE_EMBED_SUB_BATCH"])
elif os.environ.get("OPENCODE_EMBED_LOW_MEMORY", "").strip() in ("1", "true", "yes"):
    _EMBED_SUB_BATCH = 8
elif _LOW_END:
    _EMBED_SUB_BATCH = 64
elif _HIGH_END:
    _EMBED_SUB_BATCH = 128
else:
    _EMBED_SUB_BATCH = 96


def get_embed_workers_gpu(vram_mb: int | None = None) -> int:
    """VRAM-scaled embed worker count: min(6, max(2, (vram - 1024) // 600))."""
    if vram_mb is None:
        vram_mb = _get_gpu_vram_mb() or 0
    if vram_mb <= 0:
        return 2
    return min(6, max(2, (vram_mb - 1024) // 600))


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
