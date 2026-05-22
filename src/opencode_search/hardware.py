"""GPU/CPU hardware detection utilities."""

import logging
import os
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


def detect_gpu() -> dict:
    """Detect GPU capabilities via nvidia-smi.

    Returns a dict with keys:
        vendor, gpu_name, vram_mb, compute_capability, architecture,
        supports_fp16, has_tensor_cores
    Returns a dict with vendor="none" when no GPU is found.
    """
    result: dict = {
        "vendor": "none",
        "gpu_name": "",
        "vram_mb": None,
        "compute_capability": None,
        "architecture": "",
        "supports_fp16": False,
        "has_tensor_cores": False,
    }

    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,compute_cap",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return result

        line = proc.stdout.strip().splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            return result

        gpu_name = parts[0]
        try:
            vram_mb = int(parts[1])
        except ValueError:
            vram_mb = None

        compute_cap = parts[2]  # e.g. "8.9"

        # Determine architecture from compute capability major version.
        arch = _compute_cap_to_arch(compute_cap)

        # fp16 / tensor core support: Volta (7.x) and later.
        major = _major_cc(compute_cap)
        supports_fp16 = major is not None and major >= 7
        has_tensor_cores = major is not None and major >= 7

        result.update(
            {
                "vendor": "nvidia",
                "gpu_name": gpu_name,
                "vram_mb": vram_mb,
                "compute_capability": compute_cap,
                "architecture": arch,
                "supports_fp16": supports_fp16,
                "has_tensor_cores": has_tensor_cores,
            }
        )
    except FileNotFoundError:
        # nvidia-smi not present
        pass
    except subprocess.TimeoutExpired:
        logger.debug("nvidia-smi timed out")
    except Exception as exc:  # noqa: BLE001
        logger.debug("GPU detection error: %s", exc)

    return result


def _major_cc(compute_cap: Optional[str]) -> Optional[int]:
    """Return the major compute-capability integer, or None on parse failure."""
    if not compute_cap:
        return None
    try:
        return int(compute_cap.split(".")[0])
    except (ValueError, IndexError):
        return None


def _compute_cap_to_arch(compute_cap: str) -> str:
    """Map compute capability string to a human-readable NVIDIA architecture name."""
    major = _major_cc(compute_cap)
    if major is None:
        return "unknown"
    mapping = {
        3: "Kepler",
        5: "Maxwell",
        6: "Pascal",
        7: "Volta/Turing",
        8: "Ampere/Ada",
        9: "Hopper",
        10: "Blackwell",   # B100 datacenter
        12: "Blackwell",   # RTX 50-series (consumer)
    }
    return mapping.get(major, f"Unknown (SM {major})")


def get_embed_workers(vram_mb: Optional[int]) -> int:
    """Compute a sensible number of embedding workers given available VRAM.

    Formula: min(6, max(2, (vram_mb - 1024) // 600)) when vram_mb is known.
    Falls back to 2 when vram_mb is None or zero.
    Result is clamped to [1, 6].
    """
    if vram_mb:
        workers = min(6, max(2, (vram_mb - 1024) // 600))
    else:
        workers = 2
    return max(1, min(6, workers))


def get_cpu_count() -> int:
    """Return the number of logical CPUs available, defaulting to 2."""
    return os.cpu_count() or 2


def get_ram_mb() -> int:
    """Return total system RAM in MB.

    Tries psutil first, then falls back to parsing /proc/meminfo.
    Returns 0 on failure.
    """
    # Try psutil
    try:
        import psutil  # noqa: PLC0415

        return psutil.virtual_memory().total // (1024 * 1024)
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("psutil RAM detection error: %s", exc)

    # Fallback: /proc/meminfo
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    # Format: "MemTotal:       16384000 kB"
                    kb = int(line.split()[1])
                    return kb // 1024
    except (OSError, ValueError) as exc:
        logger.debug("Cannot read /proc/meminfo: %s", exc)

    return 0


def set_oom_score(score: int = -500) -> None:
    """Adjust the OOM-killer score for the current process.

    Silently ignores permission errors (requires root or CAP_SYS_RESOURCE).
    Score should be in [-1000, 1000]; negative values make OOM less likely.
    """
    try:
        with open("/proc/self/oom_score_adj", "w", encoding="utf-8") as fh:
            fh.write(str(score))
    except OSError as exc:
        logger.debug("Cannot set oom_score_adj: %s", exc)


def log_hardware_info() -> None:
    """Log GPU name, VRAM, and recommended worker count at INFO level."""
    gpu = detect_gpu()
    vram = gpu.get("vram_mb")
    workers = get_embed_workers(vram)

    if gpu.get("vendor") == "nvidia":
        logger.info(
            "GPU detected: %s, VRAM: %s MB, embed workers: %d",
            gpu.get("gpu_name", "unknown"),
            vram,
            workers,
        )
    else:
        # No CPU fallback is allowed — the embeddings layer will raise
        # GPUNotAvailableError at startup. We only log here for diagnostics.
        logger.error(
            "No NVIDIA GPU detected. opencode-search requires CUDA; "
            "the process will refuse to start (CPUExecutionProvider is forbidden)."
        )
