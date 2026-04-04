"""Local embedding + rerank backend (free).

Implements embeddings and reranking using open-source models via FastEmbed.

Notes:
- Models are downloaded and cached locally by FastEmbed on first use.
- No provider API keys are required.
- Models are unloaded when idle to free ~1-2 GB of RAM per project.
"""

from __future__ import annotations

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
      total_threads = workers × onnx_threads ≤ cpu_count

    Scaling (assuming 4-6 workers on high-end):
      ≤4 CPUs:  1 thread
      5-8 CPUs: 2 threads (2 workers × 2 = 4-8 threads)
      9-16 CPUs: 2 threads (4 workers × 2 = 8 threads)
      >16 CPUs: 4 threads (6 workers × 4 = 24 threads)
    """
    cpus = os.cpu_count() or 2
    if cpus <= 4:
        threads = "1"
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

    Automatically detects and tests GPU providers, falling back to CPU if needed.
    Results are cached after first detection. Thread-safe.

    Environment variables:
    - OPENCODE_ONNX_PROVIDER: Force a specific provider (e.g., "cuda", "rocm", "coreml", "cpu")
    - OPENCODE_ONNX_PROVIDERS: Comma-separated list of providers to try

    Provider priority (tested in order):
    1. CUDAExecutionProvider: NVIDIA GPUs (requires CUDA toolkit)
    2. ROCMExecutionProvider: AMD GPUs on Linux (requires ROCm)
    3. CoreMLExecutionProvider: macOS (Apple Silicon/Intel, may have model limits)
    4. DirectMLExecutionProvider: Windows with DirectX 12 GPUs
    5. CPUExecutionProvider: Universal fallback

    Returns:
        List of providers to use, or None to use ONNX defaults (CPU only).
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

    provider_map = {
        "tensorrt": ["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"],
        "cuda": ["CUDAExecutionProvider", "CPUExecutionProvider"],
        "rocm": ["ROCMExecutionProvider", "CPUExecutionProvider"],  # Legacy (ORT < 1.23)
        "coreml": ["CoreMLExecutionProvider", "CPUExecutionProvider"],
        "directml": ["DirectMLExecutionProvider", "CPUExecutionProvider"],
        "migraphx": ["MIGraphXExecutionProvider", "CPUExecutionProvider"],  # AMD preferred
        "amd": ["MIGraphXExecutionProvider", "ROCMExecutionProvider", "CPUExecutionProvider"],
        "cpu": ["CPUExecutionProvider"],
        "auto": None,  # Fall through to auto-detection
    }

    if provider_env in provider_map:
        result = provider_map[provider_env]
        if result is not None:
            log.info("Using provider from OPENCODE_ONNX_PROVIDER=%s: %s", provider_env, result)
        return result

    log.warning("Unknown OPENCODE_ONNX_PROVIDER=%s, using auto-detection", provider_env)
    return None


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
    # Note: CoreML is disabled by default because:
    # - It has a 16384 dimension limit that breaks many embedding models
    # - It may fail silently and fall back to CPU anyway
    # Users can force CoreML via OPENCODE_ONNX_PROVIDER=coreml if needed
    #
    # MIGraphX is preferred over ROCm for AMD GPUs because:
    # - ROCMExecutionProvider was deprecated and removed in ORT 1.23+
    # - MIGraphX EP is AMD's officially recommended provider for ORT 1.23+
    # - ROCm EP kept as fallback for older ORT versions (< 1.23)
    # Users can force ROCm via OPENCODE_ONNX_PROVIDER=rocm if needed
    gpu_providers = [
        (
            "TensorrtExecutionProvider",
            "NVIDIA TensorRT",
        ),  # Highest priority for NVIDIA (kernel fusion)
        ("CUDAExecutionProvider", "NVIDIA CUDA"),
        ("MIGraphXExecutionProvider", "AMD MIGraphX"),  # Primary for AMD (ORT 1.23+)
        ("ROCMExecutionProvider", "AMD ROCm"),  # Fallback for older ORT (< 1.23)
        ("DirectMLExecutionProvider", "DirectX ML"),
        # CoreML disabled due to dimension limits with embedding models
        # ("CoreMLExecutionProvider", "Apple CoreML"),
    ]

    # Build a cascading GPU provider chain: if TensorRT is primary, include CUDA
    # as an intermediate fallback before CPU. This prevents silent CPU fallback
    # when TensorRT fails on specific model shapes but CUDA would work.
    working_gpu: list[str] = []
    for provider, name in gpu_providers:
        if provider not in available:
            continue

        if _test_provider(provider):
            log.info("GPU provider passed test: %s (%s)", provider, name)
            _log_gpu_capabilities()
            working_gpu.append(provider)
        else:
            log.warning("%s available but failed runtime test, skipping", provider)

    if working_gpu:
        # Return all working GPU providers in priority order, with CPU as final fallback
        result = working_gpu + ["CPUExecutionProvider"]
        log.info("Using GPU provider chain: %s", result)
        return result

    log.info("No working GPU provider found, using CPU")
    return None


def _log_gpu_capabilities() -> None:
    """Log GPU capabilities at startup for diagnostics."""
    caps = _get_gpu_capabilities()
    log.info(
        "GPU capabilities: vendor=%s, name=%s, compute=%s, vram=%sMB, tensor_cores=%s, fp16=%s, io_binding=%s",
        caps.get("vendor") or "unknown",
        caps.get("gpu_name") or "n/a",
        caps.get("compute_capability") or "n/a",
        caps.get("vram_mb") or "unknown",
        caps.get("has_tensor_cores", False),
        caps.get("supports_fp16", False),
        _io_binding_confirmed,
    )


def _get_test_model_path() -> str:
    """Get path to bundled test model, works in both dev and PyInstaller modes."""
    import sys
    from pathlib import Path

    # PyInstaller bundles files in sys._MEIPASS
    if hasattr(sys, "_MEIPASS"):
        return str(Path(sys._MEIPASS) / "opencode_embedder" / "test_model.onnx")

    # Development mode: relative to this file
    return str(Path(__file__).parent / "test_model.onnx")


def _test_provider(provider: str) -> bool:
    """Test if a provider actually works by running a minimal inference.

    Some providers are listed as available but fail at runtime due to:
    - Driver version mismatches (e.g., ROCm 7.x vs onnxruntime built for 6.x)
    - Missing shared libraries (e.g., libhipblas.so.2)
    - Model incompatibilities (e.g., CoreML dimension limits)

    Uses a pre-bundled minimal ONNX model (identity: output = input) to avoid
    requiring the heavy 'onnx' package at runtime.

    For CUDA/ROCm providers, also tests IOBinding capability.
    """
    session = None
    try:
        import numpy as np
        import onnxruntime as ort

        model_path = _get_test_model_path()

        # Apply GPU-specific session options when testing GPU providers
        sess_options = ort.SessionOptions()
        sess_options.log_severity_level = 3  # Suppress warnings

        # Build provider-specific options for GPU providers
        gpu_providers = {
            "TensorrtExecutionProvider",
            "CUDAExecutionProvider",
            "ROCMExecutionProvider",
            "MIGraphXExecutionProvider",
            "DirectMLExecutionProvider",
        }
        if provider in gpu_providers:
            opts = _gpu_provider_options(provider)
            provider_list = [
                (provider, opts[0]),
                ("CPUExecutionProvider", opts[1]),
            ]
        else:
            provider_list = [provider, "CPUExecutionProvider"]

        session = ort.InferenceSession(model_path, sess_options, providers=provider_list)

        # Check which provider is actually being used
        active = session.get_providers()
        if provider not in active:
            log.debug("%s not in active providers: %s", provider, active)
            return False

        input_data = np.array([[1.0, 2.0, 3.0, 4.0]], dtype=np.float32)

        # Test IOBinding for GPU providers (eliminates CPU-GPU memory copies)
        _io_binding_providers = {
            "TensorrtExecutionProvider",
            "CUDAExecutionProvider",
            "ROCMExecutionProvider",
            "MIGraphXExecutionProvider",
            "DirectMLExecutionProvider",
        }
        if provider in _io_binding_providers:
            device = _device_for_provider(provider)
            binding = _create_io_binding(session, {"X": input_data}, device=device)
            if binding is not None:
                try:
                    session.run_with_iobinding(binding)
                    io_out = binding.get_outputs()
                    if io_out:
                        io_arr = io_out[0].numpy()
                        if np.allclose(io_arr, input_data):
                            log.info("%s IOBinding test passed", provider)
                            _set_io_binding_active(True)
                        else:
                            log.debug("%s IOBinding result mismatch", provider)
                except Exception as e:
                    log.debug("%s IOBinding run failed: %s", provider, e)

        # Test FP16 inference if tensor cores are detected (avoid is_gpu_available()
        # here to prevent circular lock: _test_provider → _fp16_active → is_gpu_available
        # → _get_onnx_providers → deadlock on _provider_lock)
        env_fp16 = os.environ.get("OPENCODE_ONNX_FP16", "auto").lower().strip()
        caps_ready = _caps_done  # only use already-detected caps to avoid blocking
        if env_fp16 not in ("0", "false", "off") and caps_ready:
            if _caps and _caps.get("has_tensor_cores"):
                fp16_data = input_data.astype(np.float16)
                try:
                    fp16_result = session.run(None, {"X": fp16_data})
                    if fp16_result:
                        _set_fp16_confirmed(True)
                        log.info("%s FP16 inference test passed — enabled", provider)
                except Exception as e:
                    log.debug("%s FP16 test skipped: %s", provider, e)

        # Standard FP32 inference test
        result = session.run(None, {"X": input_data})

        if result and np.allclose(result[0], input_data):
            return True

        log.debug("%s inference result mismatch", provider)
        return False

    except Exception as e:
        log.warning("%s test failed: %s", provider, e)
        return False
    finally:
        if session is not None:
            del session
            import gc

            gc.collect()


def _embedder(model: str):
    global _cached_embedder, _cached_embedder_model
    # Use default model when model parameter is empty
    if not model:
        model = DEFAULT_EMBED_MODEL
    with _embedder_lock:
        if _cached_embedder is not None and _cached_embedder_model == model:
            return _cached_embedder

        # Release old embedder first
        if _cached_embedder is not None:
            del _cached_embedder
            import gc

            gc.collect()

        from fastembed import TextEmbedding

        providers = _get_onnx_providers()
        log.info(
            "Loading embedding model: %s with providers: %s",
            model,
            providers or "default (CPU)",
        )

        # Build GPU provider options when a GPU provider is active
        opts: list[dict] | None = None
        if providers and len(providers) >= 2:
            gpu = providers[0]
            gpu_set = {
                "TensorrtExecutionProvider",
                "CUDAExecutionProvider",
                "ROCMExecutionProvider",
                "MIGraphXExecutionProvider",
                "DirectMLExecutionProvider",
            }
            if gpu in gpu_set:
                opts = _gpu_provider_options(gpu)
                log.debug("Embedding provider options for %s: %s", gpu, opts[0])

        try:
            embedder = TextEmbedding(model_name=model, providers=providers, provider_options=opts)
        except TypeError:
            # Older fastembed versions don't accept provider_options
            embedder = TextEmbedding(model_name=model, providers=providers)

        # Verify which provider is actually being used by the ONNX session
        _verify_onnx_session_provider(embedder, "embedder")

        # Cap token length to prevent O(n²) attention memory explosion in ONNX.
        # Without this, a 16 000-char chunk (~6 000 tokens) uses ~10 GB workspace.
        # With 1024-token truncation, workspace is ~19 MB.
        try:
            embedder.model.tokenizer.enable_truncation(max_length=_MAX_TOKENS)
        except (AttributeError, Exception):
            pass  # graceful fallback if fastembed internals change
        _cached_embedder = embedder
        _cached_embedder_model = model
        return embedder


def _verify_onnx_session_provider(model_obj, name: str) -> None:
    """Verify and log the actual ONNX session provider being used.

    This inspects the FastEmbed model's internal ONNX session to confirm
    which execution provider is active (GPU vs CPU).
    """
    try:
        # FastEmbed stores the ONNX session in model.model.model
        session = None
        if hasattr(model_obj, "model"):
            inner = model_obj.model
            if hasattr(inner, "model"):
                session = inner.model
            elif hasattr(inner, "session"):
                session = inner.session

        if session is None:
            log.warning("[%s] Could not access ONNX session for verification", name)
            return

        active_providers = session.get_providers()
        log.info("[%s] ONNX session active providers: %s", name, active_providers)

        # Check if GPU is actually being used
        gpu_providers = {
            "TensorrtExecutionProvider",
            "CUDAExecutionProvider",
            "ROCMExecutionProvider",
            "DirectMLExecutionProvider",
            "MIGraphXExecutionProvider",
            "CoreMLExecutionProvider",
        }
        using_gpu = any(p in gpu_providers for p in active_providers)

        if using_gpu:
            gpu_name = next(p for p in active_providers if p in gpu_providers)
            log.info("[%s] GPU ACTIVE: Using %s for inference", name, gpu_name)
        else:
            global _gpu_degraded, _gpu_degraded_reason
            _gpu_degraded = True
            _gpu_degraded_reason = (
                f"[{name}] GPU provider requested but ONNX session fell back to CPU. "
                f"Active providers: {active_providers}"
            )
            log.error(
                "[%s] GPU DEGRADED: No GPU provider active! Providers: %s. "
                "This violates the GPU requirement.",
                name,
                active_providers,
            )

    except Exception as e:
        log.warning("[%s] Failed to verify ONNX provider: %s", name, e)


def _reranker(model: str):
    global _cached_reranker, _cached_reranker_model
    # Use default (budget tier) model when model parameter is empty
    if not model:
        model = DEFAULT_RERANK_MODEL
    with _reranker_lock:
        if _cached_reranker is not None and _cached_reranker_model == model:
            return _cached_reranker

        # Release old reranker first
        if _cached_reranker is not None:
            del _cached_reranker
            import gc

            gc.collect()

        from fastembed.rerank.cross_encoder import TextCrossEncoder

        providers = _get_onnx_providers()
        log.info(
            "Loading reranker model: %s with providers: %s",
            model,
            providers or "default (CPU)",
        )

        # Build GPU provider options when a GPU provider is active
        opts: list[dict] | None = None
        if providers and len(providers) >= 2:
            gpu = providers[0]
            gpu_set = {
                "TensorrtExecutionProvider",
                "CUDAExecutionProvider",
                "ROCMExecutionProvider",
                "MIGraphXExecutionProvider",
                "DirectMLExecutionProvider",
            }
            if gpu in gpu_set:
                opts = _gpu_provider_options(gpu)
                log.debug("Reranker provider options for %s: %s", gpu, opts[0])

        try:
            reranker = TextCrossEncoder(
                model_name=model, providers=providers, provider_options=opts
            )
        except TypeError:
            # Older fastembed versions don't accept provider_options
            reranker = TextCrossEncoder(model_name=model, providers=providers)

        # Verify which provider is actually being used
        _verify_onnx_session_provider(reranker, "reranker")

        _cached_reranker = reranker
        _cached_reranker_model = model
        return reranker


def is_gpu_available() -> bool:
    """Check if a GPU provider is available and working.

    Returns True if CUDA, ROCm, or another GPU provider is detected and functional.
    This is used to auto-configure worker counts (more workers for GPU).
    """
    providers = _get_onnx_providers()
    if providers is None:
        return False
    # Check if any GPU provider is in the list (not just CPU)
    gpu_providers = {
        "TensorrtExecutionProvider",
        "CUDAExecutionProvider",
        "ROCMExecutionProvider",
        "DirectMLExecutionProvider",
        "MIGraphXExecutionProvider",
        "CoreMLExecutionProvider",
    }
    return any(p in gpu_providers for p in providers)


def get_active_provider() -> str:
    """Get the name of the active execution provider.

    Returns 'tensorrt', 'cuda', 'rocm', 'directml', 'coreml', or 'cpu'.
    """
    providers = _get_onnx_providers()
    if providers is None:
        return "cpu"

    provider_map = {
        "TensorrtExecutionProvider": "tensorrt",
        "CUDAExecutionProvider": "cuda",
        "ROCMExecutionProvider": "rocm",
        "DirectMLExecutionProvider": "directml",
        "MIGraphXExecutionProvider": "migraphx",
        "CoreMLExecutionProvider": "coreml",
    }

    for p in providers:
        if p in provider_map:
            return provider_map[p]
    return "cpu"


def cleanup_models() -> None:
    """Unload cached ONNX models to free ~1-2 GB of RAM.

    Call this after completing a batch of work (initial indexing, watch batch)
    when the embedder will be idle.  Models reload on-demand (~2-5s) when
    the next embedding or reranking call is made.
    """
    import gc

    global _cached_embedder, _cached_embedder_model
    global _cached_reranker, _cached_reranker_model
    _cached_embedder = None
    _cached_embedder_model = None
    _cached_reranker = None
    _cached_reranker_model = None

    # Force garbage collection to release ONNX session memory.
    # Python's GC doesn't always collect large C-extension objects promptly.
    gc.collect()
    gc.collect()  # second pass for weak refs / pointers

    # Hint to glibc to return freed pages to the OS (Linux only).
    # Without this, RSS stays high even after Python objects are collected
    # because glibc's malloc arena caches freed pages.
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except Exception:
        pass  # not Linux, or libc unavailable — skip silently


# Memory-efficient batch sizes optimized for CPU-bound workloads.
# Key insight: CPU is the bottleneck, not batch size. Larger batches don't help
# much but use significantly more memory. Sweet spot is batch_size=8-16.
#
# Memory scaling (approximate with 1024-token truncation):
#   batch_size=1:  ~19 MB workspace
#   batch_size=8:  ~150 MB workspace
#   batch_size=16: ~300 MB workspace
#   batch_size=32: ~600 MB workspace (diminishing returns)
_cpus = os.cpu_count() or 2
_ram_mb = 8192  # default assumption
try:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                _ram_mb = int(line.split()[1]) // 1024
                break
except Exception:
    # macOS fallback
    try:
        import subprocess

        out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)
        _ram_mb = int(out.strip()) // (1024 * 1024)
    except Exception:
        pass  # keep default 8GB assumption

# Performance tiers based on hardware:
#   LOW_END:  ≤4 CPUs or ≤8 GB RAM  -> conservative settings
#   STANDARD: >4 CPUs and >8 GB RAM -> balanced settings
#   HIGH_END: ≥16 CPUs and ≥32 GB RAM -> optimized (not aggressive, CPU-bound anyway)
_LOW_END = _cpus <= 4 or _ram_mb <= 8192
_HIGH_END = _cpus >= 16 and _ram_mb >= 32768

# Batch size configuration:
# - CPU: batch_size=8 is optimal (better cache utilization)
# - GPU: auto-scaled based on VRAM (GPUs excel at parallel ops)
if _LOW_END:
    _EMBED_SUB_BATCH = 64  # Texts per sub-batch
    _ONNX_BATCH_SIZE = 8  # Default, overridden by _get_onnx_batch_size()
elif _HIGH_END:
    _EMBED_SUB_BATCH = 128  # Good batching without excess memory
    _ONNX_BATCH_SIZE = 8  # Default, overridden by _get_onnx_batch_size()
else:
    _EMBED_SUB_BATCH = 96  # Standard
    _ONNX_BATCH_SIZE = 8  # Default, overridden by _get_onnx_batch_size()


def _get_gpu_vram_mb() -> int | None:
    """Detect GPU VRAM in MB. Returns None if not available."""
    import subprocess

    # Try ROCm first (AMD GPUs)
    try:
        result = subprocess.run(
            ["rocm-smi", "--showmeminfo", "vram", "--csv"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            # Parse CSV output: device,vram_total,vram_used
            for line in result.stdout.strip().split("\n")[1:]:
                parts = line.split(",")
                if len(parts) >= 2:
                    # VRAM is in bytes, convert to MB
                    vram_bytes = int(parts[1])
                    return vram_bytes // (1024 * 1024)
    except (subprocess.SubprocessError, FileNotFoundError, ValueError):
        pass

    # Try nvidia-smi (NVIDIA GPUs)
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            # Output is in MB
            return int(result.stdout.strip().split("\n")[0])
    except (subprocess.SubprocessError, FileNotFoundError, ValueError):
        pass

    return None


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
        # Blackwell (SM 12.0+) benefits from NHWC layout for better tensor core utilization
        cc = caps.get("compute_capability")
        if cc:
            try:
                if float(cc) >= 12.0:
                    opts["prefer_nhwc"] = "1"
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


def _get_onnx_batch_size() -> int:
    """Get optimal ONNX batch size based on GPU specs.

    Conservative scaling to avoid OOM with concurrent requests:
    - No GPU or <4GB: 8 (CPU-optimized)
    - 4-8GB VRAM: 8
    - 8-16GB VRAM: 12
    - 16-24GB VRAM: 16
    - >24GB VRAM: 16

    Note: With 16 concurrent connections, total GPU memory usage can be
    batch_size * 16 * sequence_length. Smaller batches are more reliable.
    """
    if not is_gpu_available():
        return 8  # CPU optimal

    vram_mb = _get_gpu_vram_mb()
    if vram_mb is None:
        log.info("Could not detect GPU VRAM, using default batch_size=12")
        return 12

    vram_gb = vram_mb / 1024

    if vram_gb < 4:
        batch_size = 8
    elif vram_gb < 8:
        batch_size = 8
    elif vram_gb < 16:
        batch_size = 12
    elif vram_gb < 24:
        batch_size = 16
    else:
        batch_size = 16  # Cap at 16 for stability with concurrent requests

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

    import time

    t_start = time.perf_counter()
    out: list[list[float]] = []
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

    t_prefix = time.perf_counter()
    # Accumulate all ONNX outputs first, then normalize in a single pass
    all_items: list = []
    for start in range(0, len(texts), _EMBED_SUB_BATCH):
        batch = texts[start : start + _EMBED_SUB_BATCH]
        prefixed = [f"passage: {t}" for t in batch]
        t_embed_start = time.perf_counter()
        items = list(embedder.embed(prefixed, batch_size=get_onnx_batch_size()))
        t_embed_done = time.perf_counter()
        all_items.extend(items)

    # Single-pass vectorized normalize across the full result matrix
    if _HAS_NUMPY and all_items:
        mat = np.asarray(all_items, dtype=np.float32)
        if mat.ndim == 1:
            mat = mat.reshape(1, -1)
        if dimensions > 0:
            if mat.shape[1] > dimensions:
                mat = mat[:, :dimensions]
            elif mat.shape[1] < dimensions:
                tmp = np.zeros((mat.shape[0], dimensions), dtype=np.float32)
                tmp[:, : mat.shape[1]] = mat
                mat = tmp
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        np.divide(mat, norms, out=mat, where=norms > 0)
        out = mat.tolist()
    else:
        for item in all_items:
            out.append(_normalize(_resize([float(x) for x in item.tolist()], dimensions)))
    t_postprocess = time.perf_counter()

    t_end = time.perf_counter()
    if log.isEnabledFor(logging.INFO):
        log.info(
            "embed[%s]: %d texts (%d avg chars), onnx=%.0fms, post=%.1fms, total=%.0fms",
            provider.upper(),
            len(texts),
            avg_chars,
            (t_embed_done - t_embed_start) * 1000,
            (t_postprocess - t_embed_done) * 1000,
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

    # Accumulate all ONNX outputs across sub-batches, then normalize once
    all_items: list = []
    for start in range(0, len(texts), _EMBED_SUB_BATCH):
        batch = texts[start : start + _EMBED_SUB_BATCH]
        prefixed = [f"passage: {t}" for t in batch]

        t_embed_start = time.perf_counter()
        items = list(embedder.embed(prefixed, batch_size=get_onnx_batch_size()))
        t_embed_done = time.perf_counter()
        t_embed_total += t_embed_done - t_embed_start
        all_items.extend(items)

    # Single-pass vectorized normalize + serialize
    t_post_start = time.perf_counter()
    if _HAS_NUMPY and all_items:
        mat = np.asarray(all_items, dtype=np.float32)
        if getattr(mat, "ndim", 0) == 1:
            mat = mat.reshape(1, -1)

        if dimensions > 0:
            if mat.shape[1] > dimensions:
                mat = mat[:, :dimensions]
            elif mat.shape[1] < dimensions:
                tmp = np.zeros((mat.shape[0], dimensions), dtype=np.float32)
                tmp[:, : mat.shape[1]] = mat
                mat = tmp

        dim_out = int(mat.shape[1]) if mat.size else dimensions
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        np.divide(mat, norms, out=mat, where=norms > 0)

        mat = np.asarray(mat, dtype="<f4")
        out_bytes = mat.tobytes()
    elif all_items:
        import array

        buf = array.array("f")
        for item in all_items:
            vec = _normalize(_resize([float(x) for x in item.tolist()], dimensions))
            buf.fromlist(vec)  # type: ignore[arg-type]
        if sys.byteorder != "little":
            buf.byteswap()
        dim_out = dimensions
        out_bytes = buf.tobytes()
    else:
        out_bytes = b""

    t_post_total = time.perf_counter() - t_post_start

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
