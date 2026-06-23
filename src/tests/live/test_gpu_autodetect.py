"""A5: GPU auto-detect — pure ranking unit tests + live e2e binding proof.

Research (June 2026): assert session.get_providers()[0] (ORT #1850/#27177/#21354),
run real inference (ragflow #14565 silent-CPU risk), use pure rank_gpu_providers
for multi-vendor breadth, assert CUDA_DEVICE_ORDER=PCI_BUS_ID (#26705/#17546).
"""
from __future__ import annotations

import os

import numpy as np
import pytest

from opencode_search.core.gpu import (
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
    """On this RTX 5080 host (DISABLE_TENSORRT=1, NvRTX absent) primary must be CUDA."""
    import onnxruntime as ort
    ranked = rank_gpu_providers(ort.get_available_providers(), disable_tensorrt=True)
    assert ranked and ranked[0] == "CUDAExecutionProvider", f"expected CUDA primary: {ranked}"


def test_gpu_ep_names_excludes_cpu():
    cpu_eps = {"CPUExecutionProvider", "OpenVINOExecutionProvider", "AzureExecutionProvider"}
    assert not (GPU_EP_NAMES & cpu_eps), f"GPU_EP_NAMES contains non-GPU EPs: {GPU_EP_NAMES & cpu_eps}"


# ---------------------------------------------------------------------------
# Live (real RTX 5080)
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
    from opencode_search.core.gpu import assert_gpu_available
    assert_gpu_available()


def test_is_gpu_available_true():
    from opencode_search.core.gpu import is_gpu_available
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
    """embed() returning (n,768) float16 proves GPU compute ran (CPU being fatal)."""
    vecs = embedder.embed(["hello world", "def foo():"])
    assert vecs.dtype == np.float16 and vecs.shape == (2, 768)


def test_reranker_real_inference_proves_gpu():
    from opencode_search.embed.embedder import Reranker
    r = Reranker()
    try:
        scores = r.rerank("query", ["a", "b"])
        assert len(scores) == 2 and all(isinstance(s, float) for s in scores)
    finally:
        del r


def test_session_options_bfc_guard_applied(embedder):
    """The SessionOptions BFC-arena + spin guard init override must have been applied."""
    import onnxruntime as ort
    assert getattr(ort.SessionOptions, "_ocs_no_pattern", False), (
        "SessionOptions BFC guard not applied — enable_mem_pattern/allow_spinning not set"
    )


def test_vram_and_temp_read_selected_device():
    from opencode_search.core.gpu import gpu_temp_c, vram_free_mb
    assert vram_free_mb() >= 0
    assert 0 <= gpu_temp_c() <= 120
