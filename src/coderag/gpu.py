"""GPU-only inference, asserted four independent times.

A working CPU path silently becomes the production path: it is 30x slower and
nothing fails, so the only symptom is an engine that everyone stops using. Each
assertion below closes a different way CPU inference has actually arrived --
a wheel swap, a driver that vanished mid-session, a caller passing a device
string, and an EP that accepted the session and then declined to run it.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time

import onnxruntime as ort

from . import config

GPU_EPS = ("TensorrtExecutionProvider", "CUDAExecutionProvider")
_FATAL = (
    "FATAL: no GPU execution provider available. Available: {available}. "
    "CPU inference is forbidden -- install onnxruntime-gpu against a working "
    "CUDA driver, or stop the daemon."
)


_preloaded = False


def preload() -> None:
    """Put the CUDA and cuDNN shared libraries on the loader path.

    The CUDA runtime arrives as pip packages under `site-packages/nvidia/*/
    lib/`, which the dynamic loader does not search. Without this, ORT still
    *reports* CUDAExecutionProvider as available -- the check is for the
    provider bridge, not its dependencies -- and then fails to load
    libonnxruntime_providers_cuda.so at session creation and silently hands
    back a CPU session. That is precisely the failure `verify_session` catches,
    and this is the fix for it rather than a second guard.

    Idempotent, and called before every session rather than at import: the
    daemon loads models long after start, and an import-time side effect that
    opens CUDA libraries makes every `coderag --help` pay for a GPU.
    """
    global _preloaded
    if _preloaded:
        return
    with contextlib.suppress(Exception):
        ort.preload_dlls()
    _preloaded = True


def ep_ladder() -> list[str]:
    """The providers to offer ORT, best first, filtered to what is installed.

    TensorRT is off by default: it recompiles an engine per input shape on
    first use, which turns a cold query into a multi-minute stall for a
    throughput gain this workload never spends.
    """
    available = set(ort.get_available_providers())
    ladder = [ep for ep in GPU_EPS if ep in available]
    if config.DISABLE_TENSORRT:
        ladder = [ep for ep in ladder if ep != "TensorrtExecutionProvider"]
    return ladder


def providers() -> list[str]:
    """Assertion 1: an empty ladder raises rather than falling back to CPU.

    ORT's own behaviour is to append CPUExecutionProvider and run happily, so
    the refusal has to be ours.
    """
    ladder = ep_ladder()
    if not ladder:
        raise RuntimeError(_FATAL.format(available=sorted(ort.get_available_providers())))
    return ladder


def assert_gpu_available() -> None:
    """Assertion 2: exit the process, do not raise.

    This runs at daemon start, where a raise would be caught by uvicorn's
    lifespan handling and logged as a failed startup that still leaves a
    listening socket. A non-zero exit is the only thing systemd reads.
    """
    try:
        providers()
    except RuntimeError as exc:
        sys.stderr.write(f"{exc}\n")
        sys.exit(1)


def check_device(device: str) -> str:
    """Assertion 3: reject any caller-supplied device naming CPU.

    Takes the whole string, because "cpu", "CPU:0" and "cuda:0,cpu" have all
    been passed at some point and only the first is obviously wrong.
    """
    if "cpu" in device.lower():
        raise ValueError(f"device {device!r} names CPU; CPU inference is forbidden")
    return device


def verify_session(session: ort.InferenceSession, what: str) -> None:
    """Assertion 4: the loaded session is actually running on the GPU.

    ORT accepts a provider list it cannot honour and reports the truth only
    afterwards, so this is checked post-load. It runs for the reranker as well
    as the embedder -- the reranker is the larger model and the one that
    quietly landed on CPU when only the embedder was checked.
    """
    got = session.get_providers()
    if not got or got[0] not in GPU_EPS:
        raise RuntimeError(
            f"FATAL: {what} loaded on {got or ['nothing']}, not a GPU provider. "
            "CPU inference is forbidden."
        )


def free_vram_bytes() -> int:
    """Free VRAM, or 0 when it cannot be read.

    Shelling out to nvidia-smi rather than taking a CUDA binding: this is read
    once at model load, and a binding would be a second way to hold a CUDA
    context open in a process that already has one.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    first = out.stdout.strip().splitlines()
    return int(first[0].strip()) * 1024 * 1024 if first else 0


def gpu_temp_c() -> int:
    """Card temperature, or 0 when it cannot be read.

    0 means "no reading", and every caller treats that as cool. A thermal
    governor that stalls on a missing sensor would wedge the index on any
    machine whose nvidia-smi differs.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    first = out.stdout.strip().splitlines()
    return int(first[0].strip()) if first else 0


def cool_down(sleep=time.sleep) -> float:
    """Block until the card is under `INDEX_TEMP_C`, up to `INDEX_TEMP_WAIT_S`.

    Measured on this laptop: the GPU sat at 89 C against a T.Limit of -3, which
    is three degrees *past* the throttle point, and clocked 460 MHz of 3090 --
    so the work was already being done at roughly a sixth of the card's rate,
    just erratically and at 97 C chassis. An index has no deadline; trading a
    longer wall clock for a card that is not throttling is the cheaper side of
    that bargain, and it makes the batch time repeatable enough to measure.
    """
    if not config.INDEX_TEMP_C:
        return 0.0
    waited = 0.0
    while waited < config.INDEX_TEMP_WAIT_S:
        temp = gpu_temp_c()
        if temp < config.INDEX_TEMP_C:
            break
        sleep(config.INDEX_TEMP_POLL_S)
        waited += config.INDEX_TEMP_POLL_S
    return waited


def adaptive_batch(per_item_bytes: int, floor: int = 8, ceiling: int = 128) -> int:
    """Batch size scaled to free VRAM, honouring an explicit override.

    A fixed batch either wastes a 16 GB card or OOMs it, depending only on what
    else is resident at the time -- and what else is resident is another
    session's model, which this process cannot see.
    """
    if config.EMBED_BATCH > 0:
        return config.EMBED_BATCH
    free = free_vram_bytes()
    if free <= 0:
        return floor
    # Half of free VRAM: the arena grows during a run and never gives it back,
    # so budgeting the whole of it guarantees an OOM on the second batch.
    budget = free // 2
    return max(floor, min(ceiling, budget // max(per_item_bytes, 1)))
