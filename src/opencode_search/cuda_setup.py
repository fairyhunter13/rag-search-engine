"""CUDA / cuDNN LD_LIBRARY_PATH bootstrap for the opencode embedder.

Must be imported **before** onnxruntime or any CUDA-linked library is imported
so that the dynamic linker picks up the correct cuDNN version bundled in this
venv rather than any version inherited from the parent process environment.

Calling ``configure_cuda_paths()`` more than once is safe (idempotent).

No external dependencies — only stdlib.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# Sub-library paths relative to  <venv>/lib/pythonX.Y/site-packages/nvidia/
_NVIDIA_SUB_LIBS = [
    "cudnn/lib",
    "cublas/lib",
    "cuda_runtime/lib",
    "curand/lib",
    "cusparse/lib",
    "cufft/lib",
    "nvjitlink/lib",
    "cuda_nvrtc/lib",
    "nvtx/lib",
]

_configured = False


def configure_cuda_paths() -> None:
    """Prepend this venv's nvidia libs to LD_LIBRARY_PATH (idempotent).

    Strips any stale ``site-packages/nvidia`` paths that may have leaked in
    from other venvs (e.g. cuda-test-venv) to avoid cuDNN version mismatches
    that cause segfaults in onnxruntime-gpu.

    This is the code equivalent of the ``nvidia_ld_fix.pth`` site-packages
    hook — moving the logic here means no manual .pth installation is needed
    on fresh deployments.
    """
    global _configured
    if _configured:
        return
    _configured = True

    # Locate the venv's nvidia lib directory using sys.prefix (works inside
    # any virtualenv; also works when running as a plain script with VIRTUAL_ENV).
    py_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    nvidia_base = Path(sys.prefix) / "lib" / py_ver / "site-packages" / "nvidia"

    if not nvidia_base.exists():
        # No nvidia packages in this venv (CPU-only or system Python).
        log.debug("cuda_setup: nvidia base dir not found at %s, skipping", nvidia_base)
        return

    prepend_paths = [
        str(nvidia_base / sub)
        for sub in _NVIDIA_SUB_LIBS
        if (nvidia_base / sub).exists()
    ]

    if not prepend_paths:
        log.debug("cuda_setup: no nvidia sub-libs found under %s", nvidia_base)
        return

    existing = os.environ.get("LD_LIBRARY_PATH", "")
    # Remove stale nvidia paths from other venvs (different sys.prefix).
    filtered = [
        p for p in existing.split(os.pathsep)
        if p and "site-packages/nvidia" not in p
    ]
    new_ld = os.pathsep.join(prepend_paths + filtered)
    os.environ["LD_LIBRARY_PATH"] = new_ld
    log.debug(
        "cuda_setup: prepended %d nvidia lib paths to LD_LIBRARY_PATH",
        len(prepend_paths),
    )
