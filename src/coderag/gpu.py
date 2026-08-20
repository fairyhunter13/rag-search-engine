"""GPU-only inference, asserted at four points on two different questions.

A working CPU path silently becomes the production path: it is 30x slower and
nothing fails, so the only symptom is an engine that everyone stops using.

Three of the assertions answer *which providers the session got* -- an empty
ladder raises rather than letting ORT append CPU, the daemon exits before it
binds a socket, and a loaded session is re-read because ORT reports the truth
only afterwards. The fourth answers a question the first three cannot see:
*where the nodes actually went*. Measured 2026-08-20, both exports place nine
shape-plumbing nodes on the CPU EP while the EP list still reads CUDA-first, so
`verify_session` passes over them. `check_placement` is the one that looks.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time

import onnxruntime as ort

from . import config

log = logging.getLogger(__name__)

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
    try:
        ort.preload_dlls()
    except Exception as exc:  # never fatal: ORT may already have found them
        # Said out loud because a silent one is the precondition of
        # `defects/a-floating-range-changed-the-cuda-major.md`.
        sys.stderr.write(f"preload_dlls failed, CUDA may be unreachable: {exc}\n")
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


def verify_session(session: ort.InferenceSession, what: str) -> None:
    """Assertion 4: the loaded session is actually running on the GPU.

    ORT accepts a provider list it cannot honour and reports the truth only
    afterwards, so this is checked post-load. It runs for the reranker as well
    as the embedder -- the reranker is the larger model and the one that
    quietly landed on CPU when only the embedder was checked.

    Necessary and not sufficient: ORT registers the CPU EP as an implicit
    fallback, so a healthy session reports `[CUDA, CPU]` and this passes however
    many individual nodes CUDA declined. `check_placement` is that half.
    """
    got = session.get_providers()
    if not got or got[0] not in GPU_EPS:
        raise RuntimeError(
            f"FATAL: {what} loaded on {got or ['nothing']}, not a GPU provider. "
            "CPU inference is forbidden."
        )
    log.info("%s bound to %s", what, got[0])


# Ops ORT pins to the CPU EP because their inputs are shape scalars, not
# tensors: a GPU kernel for them would cost two device copies to move an integer.
# Concat/Equal/Gather/Unsqueeze/Where are measured -- nine nodes, both exports,
# 2026-08-20. Range/Shape/Squeeze are the rest of the same family, listed so a
# neighbouring export does not fail for a reason we already accept.
SHAPE_ONLY_OPS = frozenset(
    {"Concat", "Equal", "Gather", "Range", "Shape", "Squeeze", "Unsqueeze", "Where"}
)

# Measured CPU share of node time: 0.059% embedder, 0.184% reranker. 1% is five
# times the worse of the two and orders of magnitude under any real tensor op.
MAX_CPU_TIME_SHARE = 0.01


def check_placement(nodes, what: str) -> None:
    """Assertion 4: no *tensor math* ran on the CPU EP.

    `nodes` is `(op_type, provider, duration_us)` per node, from an ORT profile.

    Two halves, because either alone is defeated. The op set alone is weak --
    `Gather` is on it and `Gather` is also how an embedding table is read, so a
    whole embedding lookup could hide behind an allowlisted name. The time bound
    alone is weak -- it says nothing about *which* op moved. Together, a new op
    type trips the first and a big op hiding behind an old name trips the second.
    """
    gpu_nodes = [n for n in nodes if n[1] in GPU_EPS]
    if not gpu_nodes:
        raise RuntimeError(
            f"FATAL: {what} profiled no GPU node at all; CPU inference is forbidden."
        )

    strays = sorted({op for op, ep, _ in nodes if ep not in GPU_EPS and op not in SHAPE_ONLY_OPS})
    if strays:
        raise RuntimeError(
            f"FATAL: {what} ran {strays} on {sorted({ep for _, ep, _ in nodes if ep not in GPU_EPS})}. "
            "Only shape plumbing may leave the GPU; CPU inference is forbidden."
        )

    total = sum(d for _, _, d in nodes)
    off = sum(d for _, ep, d in nodes if ep not in GPU_EPS)
    share = off / total if total else 0.0
    if share > MAX_CPU_TIME_SHARE:
        raise RuntimeError(
            f"FATAL: {what} spent {share:.2%} of node time off the GPU, over the "
            f"{MAX_CPU_TIME_SHARE:.0%} bound. CPU inference is forbidden."
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
