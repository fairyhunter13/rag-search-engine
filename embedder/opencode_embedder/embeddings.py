"""Local embedding + rerank backend (free).

Implements embeddings and reranking using open-source models via FastEmbed.

Notes:
- Models are downloaded and cached locally by FastEmbed on first use.
- No provider API keys are required.
- Models are unloaded when idle to free ~1-2 GB of RAM per project.
"""

from __future__ import annotations

# Configure CUDA/cuDNN library paths before any CUDA-linked import.
# This is the in-code equivalent of the nvidia_ld_fix.pth site hook.
from opencode_embedder.cuda_setup import configure_cuda_paths as _configure_cuda_paths
_configure_cuda_paths()

import logging
import os

log = logging.getLogger(__name__)

# Tier -> model mappings (must match Rust's cli.rs models_for_tier)
# Budget tier is the default when model parameter is empty
TIER_MODELS = {
    "premium": {
        "embed": "jinaai/jina-embeddings-v2-base-code",
        "rerank": "Xenova/ms-marco-MiniLM-L-6-v2",
    },
    "balanced": {
        "embed": "jinaai/jina-embeddings-v2-base-en",
        "rerank": "Xenova/ms-marco-MiniLM-L-6-v2",
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

# Check CuPy availability for GPU-accelerated matrix operations
_HAS_CUPY = False
try:
    import cupy as cp
    _HAS_CUPY = True
except ImportError:
    cp = None  # type: ignore[assignment]

# GPU normalization mode: "auto" (default), "gpu", "cpu"
_GPU_NORMALIZE_MODE = os.environ.get("OPENCODE_GPU_NORMALIZE", "auto").lower()

# GPU usage tracking for debugging (thread-safe)
import threading

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
    - driver_version: Driver/runtime version if available, or None
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
        "driver_version": None,
        "architecture": None,
    }

    # --- NVIDIA ---
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=compute_cap,memory.total,name,driver_version",
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
                if len(parts) >= 4:
                    result["driver_version"] = parts[3].strip()
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
    import math

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


def _normalize_embeddings_gpu(mat: "np.ndarray") -> "np.ndarray":
    """GPU-accelerated L2 normalization using CuPy.
    
    Keeps data on GPU for normalization, avoiding CPU-GPU transfers.
    Falls back to CPU if GPU fails or CuPy unavailable.
    """
    if not _HAS_CUPY:
        return _normalize_embeddings_cpu(mat)
    
    try:
        # Transfer to GPU, normalize, transfer back
        mat_gpu = cp.asarray(mat, dtype=cp.float32)
        norms = cp.linalg.norm(mat_gpu, axis=1, keepdims=True)
        cp.divide(mat_gpu, norms, out=mat_gpu, where=norms > 0)
        result = cp.asnumpy(mat_gpu)
        
        # Clean up GPU memory
        del mat_gpu, norms
        return result
    except Exception as e:
        # Fallback to CPU if GPU fails (OOM, driver issues, etc.)
        log.debug(f"GPU normalization failed, falling back to CPU: {e}")
        return _normalize_embeddings_cpu(mat)


def _normalize_embeddings_cpu(mat: "np.ndarray") -> "np.ndarray":
    """CPU L2 normalization using numpy.
    
    In-place normalization to minimize memory copies.
    """
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    np.divide(mat, norms, out=mat, where=norms > 0)
    return mat


def _normalize_embeddings(mat: "np.ndarray") -> "np.ndarray":
    """Smart L2 normalization: GPU for large batches, CPU for small.
    
    Mode controlled by OPENCODE_GPU_NORMALIZE env var:
    - "auto" (default): GPU for batches >= 256, CPU otherwise
    - "gpu": Always use GPU (if available)
    - "cpu": Always use CPU
    """
    batch_size = mat.shape[0]
    
    if _GPU_NORMALIZE_MODE == "gpu":
        if _HAS_CUPY:
            log.debug(f"normalize: {batch_size} embeddings via GPU (forced)")
            return _normalize_embeddings_gpu(mat)
        else:
            log.debug(f"normalize: GPU requested but CuPy unavailable, using CPU")
            return _normalize_embeddings_cpu(mat)
    
    elif _GPU_NORMALIZE_MODE == "cpu":
        log.debug(f"normalize: {batch_size} embeddings via CPU (forced)")
        return _normalize_embeddings_cpu(mat)
    
    else:  # auto mode
        # Use GPU for large batches (>= 256 embeddings)
        # GPU has overhead, only worth it for larger batches
        if _HAS_CUPY and batch_size >= 256:
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
_reranker_lock = threading.Lock()
_cached_reranker: object | None = None
_cached_reranker_model: str | None = None


def _build_provider_list_with_options(providers: list[str]) -> list:
    """Inject per-provider option dicts into a provider list for ORT.

    ORT accepts providers as either plain strings or (name, options_dict) tuples.
    Injecting options here ensures CUDA memory limits, cudnn algo search, and
    Blackwell compat flags are always applied regardless of list length.

    Bug fix: the old code guarded on `len(providers) >= 2`, which silently
    skipped options when OPENCODE_ONNX_PROVIDER=cuda returned a single-item
    list before GPU provider options were applied.
    """
    _GPU_EP_NAMES = {
        "TensorrtExecutionProvider",
        "CUDAExecutionProvider",
        "ROCMExecutionProvider",
        "MIGraphXExecutionProvider",
        "DirectMLExecutionProvider",
    }
    result: list = []
    for p in providers:
        if p in _GPU_EP_NAMES:
            opts = _gpu_provider_options(p)
            log.debug("Provider options for %s: %s", p, opts[0])
            result.append((p, opts[0]))
        else:
            result.append(p)
    return result


def _embedder(model: str):
    """Return (and cache) a FastEmbed TextEmbedding model loaded with GPU providers."""
    global _cached_embedder, _cached_embedder_model
    if not model:
        model = DEFAULT_EMBED_MODEL
    with _embedder_lock:
        if _cached_embedder is not None and _cached_embedder_model == model:
            return _cached_embedder

        # Release old model before loading new one
        if _cached_embedder is not None:
            _cached_embedder = None
            import gc; gc.collect()

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

        embedder = TextEmbedding(model_name=model, providers=provider_list)

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
    """Return (and cache) a FastEmbed TextCrossEncoder model loaded with GPU providers."""
    global _cached_reranker, _cached_reranker_model
    if not model:
        model = DEFAULT_RERANK_MODEL
    with _reranker_lock:
        if _cached_reranker is not None and _cached_reranker_model == model:
            return _cached_reranker

        if _cached_reranker is not None:
            del _cached_reranker
            import gc; gc.collect()

        from fastembed.rerank.cross_encoder import TextCrossEncoder

        providers = _get_onnx_providers()
        log.info(
            "Loading reranker model: %s with providers: %s",
            model,
            providers or "default (CPU)",
        )

        # Bug fix: always inject provider options regardless of list length.
        provider_list = _build_provider_list_with_options(providers) if providers else None

        reranker = TextCrossEncoder(model_name=model, providers=provider_list)

        # Verify which provider the ONNX session actually selected
        _verify_onnx_session_provider(reranker, "reranker")

        _cached_reranker = reranker
        _cached_reranker_model = model
        return reranker


def cleanup_models() -> None:
    """Release cached ONNX models to free VRAM and RAM.

    Models reload on next inference call (~2-5s cost).
    Safe to call from any thread u2014 uses locks internally.
    """
    global _cached_embedder, _cached_embedder_model
    global _cached_reranker, _cached_reranker_model
    import gc

    with _embedder_lock:
        if _cached_embedder is not None:
            _cached_embedder = None
            _cached_embedder_model = None

    with _reranker_lock:
        if _cached_reranker is not None:
            _cached_reranker = None
            _cached_reranker_model = None

    gc.collect()
    gc.collect()  # second pass for weak refs
    log.info("cleanup_models: released cached embedder and reranker")


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
      total_threads = workers u00d7 onnx_threads u2264 cpu_count

    Scaling (assuming 4-6 workers on high-end):
      low-memory mode: 1 thread (minimise memory arenas)
      u22644 CPUs:  1 thread
      5-8 CPUs: 2 threads (2 workers u00d7 2 = 4-8 threads)
      9-16 CPUs: 2 threads (4 workers u00d7 2 = 8 threads)
      >16 CPUs: 4 threads (6 workers u00d7 4 = 24 threads)
    """
    cpus = os.cpu_count() or 2
    low_memory = os.environ.get("OPENCODE_EMBED_LOW_MEMORY", "").strip() in ("1", "true", "yes")
    if low_memory:
        threads = "1"  # single thread minimises per-thread memory arenas
    elif cpus <= 4:
        threads = "1"
    elif cpus <= 8:
        threads = "2"
    elif cpus <= 16:
        threads = "2"
    else:
        threads = "4"
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
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
import threading

_detected_providers: list[str] | None = None
_provider_detection_done: bool = False
_provider_lock = threading.Lock()


def _get_onnx_providers() -> list[str] | None:
    """Get optimal ONNX execution providers for this system.

    Automatically detects and tests GPU providers. When OPENCODE_GPU_REQUIRED=1,
    raises RuntimeError immediately if no working GPU provider is found — no CPU fallback.
    Results are cached after first detection. Thread-safe.

    Environment variables:
    - OPENCODE_GPU_REQUIRED: If "1", raise on no GPU (no CPU fallback ever)
    - OPENCODE_ONNX_PROVIDER: Force a specific provider (e.g., "cuda", "rocm", "coreml", "cpu")
    - OPENCODE_ONNX_PROVIDERS: Comma-separated list of providers to try
    - OPENCODE_DISABLE_TENSORRT: If "1", skip TensorRT (Blackwell compat)

    Provider priority (tested in order):
    1. CUDAExecutionProvider: NVIDIA GPUs (stable, all generations)
    2. TensorrtExecutionProvider: NVIDIA TensorRT (if not disabled)
    3. MIGraphXExecutionProvider: AMD GPUs on Linux (ORT 1.23+)
    4. ROCMExecutionProvider: AMD GPUs (older ORT fallback)
    5. DirectMLExecutionProvider: Windows with DirectX 12 GPUs
    6. CPU: NOT AN OPTION u2014 raises RuntimeError if no GPU is found

    Returns:
        List of providers to use, or None to use ONNX defaults (CPU only).
        Never returns CPU-only when OPENCODE_GPU_REQUIRED=1.
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
    gpu_set = {
        "TensorrtExecutionProvider",
        "CUDAExecutionProvider",
        "ROCMExecutionProvider",
        "MIGraphXExecutionProvider",
        "DirectMLExecutionProvider",
        "CoreMLExecutionProvider",
    }
    return any(p in gpu_set for p in providers)


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

    # GPU-only provider map u2014 CPU is not an option.
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
    """Raised when OPENCODE_GPU_REQUIRED=1 but no working GPU provider is found."""


def _raise_no_gpu(available: list[str], tested: list[str]) -> None:
    """Raise GPUNotAvailableError with a clear diagnostic message.

    Called only when OPENCODE_GPU_REQUIRED=1 and all GPU providers failed their
    runtime test. CPU fallback is explicitly forbidden in this mode.
    """
    msg = (
        "[GPU-REQUIRED] FATAL: OPENCODE_GPU_REQUIRED=1 but no working GPU execution "
        "provider was found. CPU fallback is FORBIDDEN.\n"
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
        "To allow CPU fallback (not recommended): unset OPENCODE_GPU_REQUIRED\n"
        "To diagnose: python -c \"import onnxruntime; print(onnxruntime.get_available_providers())\""
    )
    log.critical(msg)
    raise GPUNotAvailableError(msg)


def assert_gpu_available() -> None:
    """Startup check: verify GPU is available and working.

    Call once at process startup when OPENCODE_GPU_REQUIRED=1 so the server
    fails fast with a clear message rather than silently running on CPU.

    Raises GPUNotAvailableError if GPU is required but unavailable.
    Does nothing if OPENCODE_GPU_REQUIRED is not set.
    """
    gpu_required = os.environ.get("OPENCODE_GPU_REQUIRED", "0").lower() in ("1", "true", "yes")
    if not gpu_required:
        return

    log.info("[GPU-REQUIRED] Startup GPU check (OPENCODE_GPU_REQUIRED=1)")

    # Probe without loading any model u2014 forces provider detection
    providers = _get_onnx_providers()  # may raise GPUNotAvailableError internally

    # Double-check: the returned list must contain at least one GPU provider
    gpu_provider_names = {
        "TensorrtExecutionProvider",
        "CUDAExecutionProvider",
        "ROCMExecutionProvider",
        "DirectMLExecutionProvider",
        "MIGraphXExecutionProvider",
        "CoreMLExecutionProvider",
    }
    if providers is None or not any(p in gpu_provider_names for p in providers):
        _raise_no_gpu(
            available=_onnx_available_providers(),
            tested=list(gpu_provider_names),
        )

    active = next((p for p in (providers or []) if p in gpu_provider_names), "none")
    log.info("[GPU-REQUIRED] Startup GPU check PASSED u2014 active provider: %s", active)


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

    # Check for environment variable override first
    env_providers = _parse_provider_env()
    if env_providers is not None:
        return env_providers

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
        # CoreML disabled due to dimension limits with embedding models
        # ("CoreMLExecutionProvider", "Apple CoreML"),
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

    # Determine if strict GPU-only mode is active.
    # Sources (checked in order, first truthy wins):
    #   1. OPENCODE_GPU_REQUIRED env var ("1")
    #   2. CLAUDE.md rule: all ONNX inference MUST run on GPU
    gpu_required = os.environ.get("OPENCODE_GPU_REQUIRED", "0").lower() in ("1", "true", "yes")

    if working_gpu:
        log.info("Using GPU-only provider chain: %s", working_gpu)
        return working_gpu

    # No working GPU provider found u2014 raise unconditionally, CPU is not an option.
    _raise_no_gpu(
        available=sorted(available),
        tested=[p for p, _ in gpu_providers],
    )
    return None  # unreachable; satisfies type checker


# Track whether FP16 inference was confirmed working at runtime
_fp16_runtime_confirmed = False


def _set_fp16_confirmed(val: bool) -> None:
    global _fp16_runtime_confirmed
    _fp16_runtime_confirmed = val


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
    # auto: trust runtime test result if available, else check tensor cores
    if _fp16_runtime_confirmed:
        return True
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


def _create_io_binding(session, inputs: dict, device: str = "cuda", device_id: int = 0):
    """Create IOBinding to keep tensors on GPU memory.

    Binds input/output tensors directly to GPU memory to avoid CPU-GPU transfers.
    Works with CUDA, ROCm, MIGraphX, TensorRT, and DirectML providers.

    Args:
        session: ONNX InferenceSession
        inputs: dict mapping input name -> numpy array
        device: device string ("cuda" for NVIDIA/ROCm/MIGraphX, "dml" for DirectML)
        device_id: GPU device index

    Returns:
        IOBinding object or None if IOBinding is unsupported.
    """
    try:
        import onnxruntime as ort

        binding = session.io_binding()
        for name, arr in inputs.items():
            val = ort.OrtValue.ortvalue_from_numpy(arr, device, device_id)
            binding.bind_ortvalue_input(name, val)
        for out in session.get_outputs():
            binding.bind_output(out.name, device)
        return binding
    except Exception as e:
        log.debug("IOBinding creation failed: %s", e)
        return None


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
        
        # Create IOBinding for GPU tensors
        binding = session.io_binding()
        
        # Bind inputs to GPU
        input_ids_gpu = ort.OrtValue.ortvalue_from_numpy(input_ids, device, device_id)
        attention_mask_gpu = ort.OrtValue.ortvalue_from_numpy(attention_mask, device, device_id)
        
        binding.bind_ortvalue_input("input_ids", input_ids_gpu)
        binding.bind_ortvalue_input("attention_mask", attention_mask_gpu)
        
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
    - 16-24GB VRAM: 16
    - >24GB VRAM: 16

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
        batch_size = 12  # Reduced from 16
    else:
        batch_size = 12  # Reduced cap from 16

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


def embed_passages(texts: list[str], *, model: str, dimensions: int) -> list[list[float]]:
    if not texts:
        return []

    import gc
    import time

    t_start = time.perf_counter()
    embedder = _embedder(model)
    t_get_embedder = time.perf_counter()

    # Track GPU vs CPU operations
    provider = get_active_provider()
    is_gpu = provider in ("tensorrt", "cuda", "rocm", "directml", "migraphx", "coreml")
    if is_gpu:
        _increment_gpu_ops()
    else:
        _increment_cpu_ops()

    total_chars = sum(len(t) for t in texts)
    avg_chars = total_chars // len(texts) if texts else 0

    # Try IOBinding path first if GPU available and active
    use_iobinding = is_gpu and _io_binding_confirmed
    if use_iobinding:
        try:
            # Access ONNX session from FastEmbed: embedder.model.model
            session = embedder.model.model if hasattr(embedder.model, 'model') else None
            tokenizer = embedder.model.tokenizer if hasattr(embedder.model, 'tokenizer') else None
            
            if session is not None and tokenizer is not None:
                device = _device_for_provider(provider)
                all_items: list = []
                t_embed_total = 0.0
                
                for start in range(0, len(texts), _EMBED_SUB_BATCH):
                    batch = texts[start : start + _EMBED_SUB_BATCH]
                    prefixed = [f"passage: {t}" for t in batch]
                    
                    t_embed_start = time.perf_counter()
                    # IOBinding: tensors stay on GPU
                    batch_result = _embed_batch_iobinding(
                        session, tokenizer, prefixed, 
                        get_onnx_batch_size(), device
                    )
                    t_embed_done = time.perf_counter()
                    t_embed_total += t_embed_done - t_embed_start
                    
                    if batch_result is not None:
                        # Pool: mean of last_hidden_state (common for BERT models)
                        # Shape: (batch, seq_len, hidden_dim) → (batch, hidden_dim)
                        if batch_result.ndim == 3:
                            batch_result = np.mean(batch_result, axis=1)
                        all_items.append(batch_result.astype(np.float32))
                        del batch_result
                    else:
                        # IOBinding failed, fall back to standard path
                        use_iobinding = False
                        break
                
                if use_iobinding and all_items:
                    # Success! Process the GPU results
                    mat = np.concatenate(all_items, axis=0) if len(all_items) > 1 else all_items[0]
                    del all_items
                    
                    if mat.ndim == 1:
                        mat = mat.reshape(1, -1)
                    if dimensions > 0:
                        if mat.shape[1] > dimensions:
                            mat = mat[:, :dimensions]
                        elif mat.shape[1] < dimensions:
                            tmp = np.zeros((mat.shape[0], dimensions), dtype=np.float32)
                            tmp[:, : mat.shape[1]] = mat
                            del mat
                            mat = tmp
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
                            t_embed_total * 1000,
                            (t_end - t_start) * 1000,
                        )
                    return out
        except Exception as e:
            log.debug("IOBinding path failed, falling back to standard: %s", e)
            use_iobinding = False

    # Standard path (fallback or when IOBinding unavailable)
    t_prefix = time.perf_counter()
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
            if mat.shape[1] > dimensions:
                mat = mat[:, :dimensions]
            elif mat.shape[1] < dimensions:
                tmp = np.zeros((mat.shape[0], dimensions), dtype=np.float32)
                tmp[:, : mat.shape[1]] = mat
                del mat
                mat = tmp
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
    is_gpu = provider in ("tensorrt", "cuda", "rocm", "directml", "migraphx", "coreml")
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

    import gc
    import sys
    import time

    embedder = _embedder(model)

    provider = get_active_provider()
    is_gpu = provider in ("tensorrt", "cuda", "rocm", "directml", "migraphx", "coreml")
    if is_gpu:
        _increment_gpu_ops()
    else:
        _increment_cpu_ops()

    t_start = time.perf_counter()
    dim_out = dimensions
    t_embed_total = 0.0

    # Try IOBinding path if GPU available
    use_iobinding = is_gpu and _io_binding_confirmed
    if use_iobinding:
        try:
            session = embedder.model.model if hasattr(embedder.model, 'model') else None
            tokenizer = embedder.model.tokenizer if hasattr(embedder.model, 'tokenizer') else None
            
            if session is not None and tokenizer is not None:
                device = _device_for_provider(provider)
                all_items: list = []
                
                for start in range(0, len(texts), _EMBED_SUB_BATCH):
                    batch = texts[start : start + _EMBED_SUB_BATCH]
                    prefixed = [f"passage: {t}" for t in batch]
                    
                    t_embed_start = time.perf_counter()
                    batch_result = _embed_batch_iobinding(
                        session, tokenizer, prefixed,
                        get_onnx_batch_size(), device
                    )
                    t_embed_done = time.perf_counter()
                    t_embed_total += t_embed_done - t_embed_start
                    
                    if batch_result is not None:
                        # Pool if needed
                        if batch_result.ndim == 3:
                            batch_result = np.mean(batch_result, axis=1)
                        all_items.append(batch_result.astype(np.float32))
                        del batch_result
                    else:
                        use_iobinding = False
                        break
                    
                    gc.collect()
                
                if use_iobinding and all_items:
                    # Process on GPU, single copy to bytes
                    mat = np.concatenate(all_items, axis=0) if len(all_items) > 1 else all_items[0]
                    del all_items
                    gc.collect()
                    
                    if mat.ndim == 1:
                        mat = mat.reshape(1, -1)
                    
                    if dimensions > 0:
                        if mat.shape[1] > dimensions:
                            mat = mat[:, :dimensions]
                        elif mat.shape[1] < dimensions:
                            tmp = np.zeros((mat.shape[0], dimensions), dtype=np.float32)
                            tmp[:, : mat.shape[1]] = mat
                            del mat
                            mat = tmp
                    
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
                            t_embed_total * 1000,
                            (t_end - t_start) * 1000,
                        )
                    return out_bytes, dim_out, len(texts)
        except Exception as e:
            log.debug("IOBinding path failed for f32_bytes, falling back: %s", e)
            use_iobinding = False

    # Standard path (fallback)
    all_items: list = []
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
            if mat.shape[1] > dimensions:
                mat = mat[:, :dimensions]
            elif mat.shape[1] < dimensions:
                tmp = np.zeros((mat.shape[0], dimensions), dtype=np.float32)
                tmp[:, : mat.shape[1]] = mat
                del mat
                mat = tmp

        dim_out = int(mat.shape[1]) if mat.size else dimensions
        mat = _normalize_embeddings(mat)
        mat = np.asarray(mat, dtype="<f4")
        # Convert to bytes directly, no .tolist() intermediate step
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
    is_gpu = provider in ("tensorrt", "cuda", "rocm", "directml", "migraphx", "coreml")
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


def rerank(query: str, docs: list[str], *, model: str, top_k: int) -> list[tuple[int, float]]:
    if not docs or top_k <= 0:
        return []

    import time

    t_start = time.perf_counter()
    reranker = _reranker(model)

    # Track GPU vs CPU operations
    provider = get_active_provider()
    is_gpu = provider in ("tensorrt", "cuda", "rocm", "directml", "migraphx", "coreml")
    if is_gpu:
        _increment_gpu_ops()
    else:
        _increment_cpu_ops()

    scores = list(reranker.rerank(query, docs))
    t_end = time.perf_counter()

    # Empty scores check to prevent ValueError on min()/max()
    if not scores:
        return []

    # Vectorized score normalization using numpy (faster than list comprehension)
    if _HAS_NUMPY and len(scores) > 10:
        scores_arr = np.array(scores, dtype=np.float32)
        lo = float(scores_arr.min())
        hi = float(scores_arr.max())
        
        if hi == lo:
            normed = np.full(len(scores), 0.5, dtype=np.float32)
        else:
            normed = (scores_arr - lo) / (hi - lo)
        
        # argsort + reverse for top-k (more efficient than sorted())
        order = np.argsort(normed)[::-1][:top_k].tolist()
        normed = normed.tolist()
    else:
        # Fallback to list comprehension for small batches
        lo = min(scores)
        hi = max(scores)
        if hi == lo:
            normed = [0.5 for _ in scores]
        else:
            normed = [(s - lo) / (hi - lo) for s in scores]
        
        order = sorted(range(len(normed)), key=lambda i: normed[i], reverse=True)[:top_k]

    if log.isEnabledFor(logging.INFO):
        log.info(
            "rerank[%s]: %d docs, %.0fms",
            provider.upper(),
            len(docs),
            (t_end - t_start) * 1000,
        )
    return [(i, float(normed[i])) for i in order]


# ---------------------------------------------------------------------------
# Internal helpers expected by tests
# ---------------------------------------------------------------------------

# CPU count available to the process (used by tests and thread scaling)
_cpus: int = os.cpu_count() or 2

# RAM detection (MB) u2014 used for hardware profiling
try:
    import psutil as _psutil
    _ram_mb: int = int(_psutil.virtual_memory().total / 1024 / 1024)
except Exception:
    _ram_mb = 8192  # default 8 GB

# Hardware profile flags
_LOW_END: bool = _cpus <= 4 or _ram_mb <= 8192
_HIGH_END: bool = _cpus >= 16 and _ram_mb >= 32768

# Max tokens for ONNX inference (truncates to prevent O(n^2) attention OOM)
_MAX_TOKENS: int = 1024

# Sub-batch size for chunked ONNX inference u2014 hardware adaptive
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
    import gc
    import numpy as np

    session = None
    model_path = _get_test_model_path()
    try:
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.log_severity_level = 3

        gpu_set = {
            "TensorrtExecutionProvider",
            "CUDAExecutionProvider",
            "ROCMExecutionProvider",
            "MIGraphXExecutionProvider",
            "DirectMLExecutionProvider",
        }
        provider_list = [(provider, {})] if provider in gpu_set else [provider]

        session = ort.InferenceSession(model_path, so, providers=provider_list)
        active = session.get_providers()
        if provider not in active:
            return False

        input_data = np.array([[1.0, 2.0, 3.0, 4.0]], dtype=np.float32)
        result = session.run(None, {"X": input_data})
        return bool(result and np.allclose(result[0], input_data))

    except Exception as e:
        log.debug("%s provider test failed: %s", provider, e)
        return False
    finally:
        if session is not None:
            del session
            import gc
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
        gpu_set = {
            "TensorrtExecutionProvider", "CUDAExecutionProvider",
            "ROCMExecutionProvider", "MIGraphXExecutionProvider",
            "DirectMLExecutionProvider", "CoreMLExecutionProvider",
        }
        using_gpu = any(p in gpu_set for p in active)
        if using_gpu:
            gpu_name = next(p for p in active if p in gpu_set)
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
        log.warning("[%s] Failed to verify ONNX provider: %s", name, e)
