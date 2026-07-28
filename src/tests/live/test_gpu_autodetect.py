"""A5: GPU auto-detect — pure ranking unit tests + live e2e binding proof.

Research (June 2026): assert session.get_providers()[0] (ORT #1850/#27177/#21354),
run real inference (ragflow #14565 silent-CPU risk), use pure rank_gpu_providers
for multi-vendor breadth, assert CUDA_DEVICE_ORDER=PCI_BUS_ID (#26705/#17546).
"""
from __future__ import annotations

import itertools
import os
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from rag_search.core.gpu import (
    _GPU_EP_ORDER,
    GPU_EP_NAMES,
    rank_gpu_providers,
    select_gpu_device,
    select_gpu_providers,
)

pytestmark = pytest.mark.live

# ---------------------------------------------------------------------------
# Pure / deterministic (no GPU, safe at collection time)
# ---------------------------------------------------------------------------


def test_rank_gpu_providers_ladder_order():
    available = [*_GPU_EP_ORDER, "CPUExecutionProvider"]
    ranked = rank_gpu_providers(available, disable_tensorrt=False)
    assert ranked == list(_GPU_EP_ORDER)
    assert "CPUExecutionProvider" not in ranked


def test_rank_gpu_providers_cpu_only_returns_empty():
    """Fatal-on-CPU proof: CPU-only input → empty rank."""
    assert rank_gpu_providers(["CPUExecutionProvider"]) == []
    assert rank_gpu_providers(["CPUExecutionProvider", "OpenVINOExecutionProvider"]) == []


def test_rank_gpu_providers_disable_tensorrt():
    """disable_tensorrt drops classic Tensorrt but keeps NvRTX and CUDA."""
    ranked = rank_gpu_providers(list(_GPU_EP_ORDER), disable_tensorrt=True)
    assert "TensorrtExecutionProvider" not in ranked
    assert "NvTensorRTRTXExecutionProvider" in ranked
    assert "CUDAExecutionProvider" in ranked


def test_rank_gpu_providers_this_host_cuda_primary():
    """On CUDA GPU host (DISABLE_TENSORRT=1, NvRTX absent) primary must be CUDA."""
    import onnxruntime as ort
    ranked = rank_gpu_providers(ort.get_available_providers(), disable_tensorrt=True)
    assert ranked and ranked[0] == "CUDAExecutionProvider", f"expected CUDA primary: {ranked}"


def test_gpu_ep_names_excludes_cpu():
    cpu_eps = {"CPUExecutionProvider", "OpenVINOExecutionProvider", "AzureExecutionProvider"}
    assert not (GPU_EP_NAMES & cpu_eps), f"GPU_EP_NAMES contains non-GPU EPs: {GPU_EP_NAMES & cpu_eps}"


# ---------------------------------------------------------------------------
# Live (CUDA GPU)
# ---------------------------------------------------------------------------


def test_select_gpu_providers_non_empty_and_no_cpu():
    providers = select_gpu_providers()
    assert providers, "select_gpu_providers() returned empty"
    for name, _opts in providers:
        assert name != "CPUExecutionProvider"
        assert name in GPU_EP_NAMES


def test_select_gpu_providers_device_id_attached():
    dev = select_gpu_device()
    for _name, opts in select_gpu_providers():
        assert opts.get("device_id") == dev


def test_select_gpu_device_sets_pci_bus_id():
    """Resolver must set CUDA_DEVICE_ORDER=PCI_BUS_ID (ORT #26705/#17546)."""
    select_gpu_device()
    assert os.environ.get("CUDA_DEVICE_ORDER") == "PCI_BUS_ID"


def test_select_gpu_device_within_nvml_count():
    try:
        import pynvml
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetDeviceCount()
    except Exception:
        count = 1
    assert 0 <= select_gpu_device() < max(count, 1)


def test_select_gpu_providers_fatal_on_cpu_only():
    """rank_gpu_providers(["CPUExecutionProvider"]) → empty, mimicking select_gpu_providers fatal path."""
    assert rank_gpu_providers(["CPUExecutionProvider"]) == []


def test_assert_gpu_available_returns():
    from rag_search.core.gpu import assert_gpu_available
    assert_gpu_available()


def test_is_gpu_available_true():
    from rag_search.core.gpu import is_gpu_available
    assert is_gpu_available() is True


# ---------------------------------------------------------------------------
# Real-inference e2e — authoritative binding proof
# ---------------------------------------------------------------------------


def test_embedder_bound_to_gpu_ep_after_inference(embedder):
    """E2E: session.get_providers()[0] ∈ GPU_EP_NAMES after actual embed call."""
    providers = embedder._model.model.model.get_providers()
    assert providers and providers[0] in GPU_EP_NAMES, f"not on GPU: {providers}"
    assert providers[0] != "CPUExecutionProvider"


def test_embedder_real_inference_proves_gpu(embedder):
    """Real inference returning (n,768) proves compute ran — and _init() already refused to
    return an embedder whose primary EP wasn't a GPU one, so that compute was on the GPU.

    The dtype is not part of that proof: it comes from our own cast in embed(), not from the
    execution provider. It is asserted here only to pin the float32 contract store.insert wants.
    """
    vecs = embedder.embed(["hello world", "def foo():"])
    assert vecs.dtype == np.float32 and vecs.shape == (2, 768)


def test_reranker_real_inference_proves_gpu():
    from rag_search.embed.embedder import Reranker
    r = Reranker()
    try:
        scores = r.rerank("query", ["a", "b"])
        assert len(scores) == 2 and all(isinstance(s, float) for s in scores)
    finally:
        del r


def test_session_options_bfc_guard_applied(embedder):
    """The SessionOptions BFC-arena + spin guard init override must have been applied."""
    import onnxruntime as ort
    assert getattr(ort.SessionOptions, "_rse_no_pattern", False), (
        "SessionOptions BFC guard not applied — enable_mem_pattern/allow_spinning not set"
    )


def test_vram_and_temp_read_selected_device():
    from rag_search.core.gpu import gpu_temp_c, vram_free_mb
    assert vram_free_mb() >= 0
    assert 0 <= gpu_temp_c() <= 120


# ---------------------------------------------------------------------------
# TK6: GPU inference is serialised, and the temperature-pacing layer is gone
# ---------------------------------------------------------------------------


class _Timed:
    """The real fastembed model, wrapped to record when each inference actually ran.

    Every call reaches the real session — this records, it does not stand in. It has to
    sit here rather than around Embedder.embed() because the interval TK6a asserts on is
    the one *inside* _GPU_INFER_LOCK; timing from outside would measure the wait too.
    """

    def __init__(self, real, spans, guard):
        self._real, self._spans, self._guard = real, spans, guard

    def _run(self, label, call):
        t0 = time.perf_counter()
        out = list(call())
        with self._guard:
            self._spans.append((label, t0, time.perf_counter()))
        return out

    def embed(self, texts, batch_size=8):
        return self._run("embed", lambda: self._real.embed(texts, batch_size=batch_size))

    def rerank(self, query, passages):
        return self._run("rerank", lambda: self._real.rerank(query, passages))


def _embed_loop(embedder) -> None:
    for _ in range(4):
        embedder.embed(["def foo():", "class Bar:"])


def _rerank_loop(reranker) -> None:
    for _ in range(4):
        reranker.rerank("query", ["alpha", "beta"])


def test_tk6b_no_temperature_pacing_remains_in_src():
    """TK6b: the deleted layer stays deleted, while gpu_temp_c survives as observability.

    This is what stops it growing back one call site at a time — which is how the
    hardcoded 78 reached sweeps.py after the constants had already been centralised.
    """
    import rag_search
    banned = ("THERMAL", "_thermal_pace", "_await_thermal_headroom", "thermal_guard_fn")
    root = Path(rag_search.__file__).parent
    offenders = [
        f"{p.relative_to(root)}:{tok}"
        for p in sorted(root.rglob("*.py"))
        for tok in banned
        if tok in p.read_text()
    ]
    assert not offenders, f"temperature-pacing layer is growing back: {offenders}"
    # The trailing paren is load-bearing: without it a rename to gpu_temp_c_anything
    # still satisfies the substring, so the assertion would pass on the very deletion
    # it exists to catch. Demonstrated — the prefix form went green on a renamed def.
    assert "def gpu_temp_c(" in (root / "core" / "gpu.py").read_text(), (
        "gpu_temp_c was deleted with the pacing layer — it is observability and stays"
    )


def test_tk6a_gpu_inference_regions_never_overlap(embedder):
    """TK6a: concurrent embed + rerank hold one card, so their inference spans are disjoint.

    onnxruntime#26610 — two sessions on one device driven from different threads abort or
    SEGV, and Run() releases the GIL so daemon threads genuinely overlap. Asserting "no
    crash" would not discriminate: the SEGV was intermittent under 228 tasks.
    """
    from rag_search.embed.embedder import Reranker
    spans: list[tuple[str, float, float]] = []
    guard = threading.Lock()
    reranker = Reranker()
    reranker._init()
    real_embed, real_rerank = embedder._model, reranker._model
    embedder._model = _Timed(real_embed, spans, guard)
    reranker._model = _Timed(real_rerank, spans, guard)
    try:
        threads = [
            threading.Thread(target=_embed_loop, args=(embedder,)) for _ in range(3)
        ] + [threading.Thread(target=_rerank_loop, args=(reranker,)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        assert not any(t.is_alive() for t in threads), "a GPU worker never finished"
    finally:
        embedder._model, reranker._model = real_embed, real_rerank
        del reranker

    assert len(spans) == 20, f"expected 20 inference calls, recorded {len(spans)}"
    ordered = sorted(spans, key=lambda s: s[1])
    overlaps = [(a, b) for a, b in itertools.pairwise(ordered) if b[1] < a[2]]
    assert not overlaps, f"GPU inference regions overlapped: {overlaps}"
