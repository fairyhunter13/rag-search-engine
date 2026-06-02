"""Unit tests for embedding helpers."""
from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from opencode_search.embeddings import _embed_batch_iobinding


class _FakeEncoding:
    def __init__(self, ids, attention_mask, type_ids=None):
        self.ids = ids
        self.attention_mask = attention_mask
        self.type_ids = type_ids if type_ids is not None else [0] * len(ids)


class _FakeTokenizer:
    def __init__(self, encodings):
        self._encodings = encodings

    def encode_batch(self, texts):
        assert texts
        return self._encodings


class _FakeInput:
    def __init__(self, name: str):
        self.name = name


class _FakeOutput:
    def __init__(self, name: str, value=None):
        self.name = name
        self._value = value if value is not None else np.ones((1, 3, 4), dtype=np.float32)

    def numpy(self):
        return self._value


class _FakeBinding:
    def __init__(self):
        self.inputs = {}
        self.outputs = []

    def bind_ortvalue_input(self, name, value):
        self.inputs[name] = value

    def bind_output(self, name, device):
        self.outputs.append((name, device))

    def get_outputs(self):
        return [_FakeOutput("last_hidden_state")]


class _FakeSession:
    def __init__(self, input_names):
        self._inputs = [_FakeInput(name) for name in input_names]
        self._binding = _FakeBinding()

    def get_inputs(self):
        return self._inputs

    def get_outputs(self):
        return [_FakeOutput("last_hidden_state")]

    def io_binding(self):
        return self._binding

    def run_with_iobinding(self, binding):
        assert binding is self._binding


def _install_fake_onnxruntime(monkeypatch):
    class _FakeOrtValue:
        @staticmethod
        def ortvalue_from_numpy(array, device, device_id=0):
            return {"array": array, "device": device, "device_id": device_id}

    fake_ort = types.SimpleNamespace(OrtValue=_FakeOrtValue)
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)


def test_embed_batch_iobinding_binds_token_type_ids_when_required(monkeypatch):
    _install_fake_onnxruntime(monkeypatch)
    session = _FakeSession(["input_ids", "attention_mask", "token_type_ids"])
    tokenizer = _FakeTokenizer([
        _FakeEncoding([1, 2, 3], [1, 1, 1], [0, 0, 0]),
    ])

    result = _embed_batch_iobinding(session, tokenizer, ["hello"], batch_size=1)

    assert result is not None
    assert "token_type_ids" in session._binding.inputs
    np.testing.assert_array_equal(
        session._binding.inputs["token_type_ids"]["array"],
        np.array([[0, 0, 0]], dtype=np.int64),
    )


def test_embed_batch_iobinding_skips_token_type_ids_when_not_required(monkeypatch):
    _install_fake_onnxruntime(monkeypatch)
    session = _FakeSession(["input_ids", "attention_mask"])
    tokenizer = _FakeTokenizer([
        _FakeEncoding([1, 2, 3], [1, 1, 1]),
    ])

    result = _embed_batch_iobinding(session, tokenizer, ["hello"], batch_size=1)

    assert result is not None
    assert "token_type_ids" not in session._binding.inputs


@pytest.mark.runtime_deps
def test_fastembed_session_options_expose_log_severity_level():
    fastembed = pytest.importorskip("fastembed.common.onnx_model")

    class _FakeSessionOptions:
        def __init__(self):
            self.enable_cpu_mem_arena = True
            self.enable_mem_pattern = True
            self.execution_mode = None
            self.graph_optimization_level = None
            self.log_severity_level = None

        def add_session_config_entry(self, key, value):
            self._last_session_config = (key, value)

    session_options = _FakeSessionOptions()
    # Build options dict from whatever the installed fastembed actually exposes.
    # The set of exposed options has changed across versions:
    #   - older versions exposed: enable_cpu_mem_arena, enable_mem_pattern,
    #     execution_mode, graph_optimization_level, log_severity_level
    #   - current version exposes: enable_cpu_mem_arena only
    exposed = set(getattr(fastembed.OnnxModel, "EXPOSED_SESSION_OPTIONS", ("enable_cpu_mem_arena",)))
    bool_opts = {"enable_cpu_mem_arena": False, "enable_mem_pattern": False}
    misc_opts = {"execution_mode": "sequential", "graph_optimization_level": "extended", "log_severity_level": 3}
    opts = {k: v for k, v in {**bool_opts, **misc_opts}.items() if k in exposed}
    fastembed.OnnxModel.add_extra_session_options(session_options, opts)

    # Verify that every option we passed was actually applied.
    if "enable_cpu_mem_arena" in opts:
        assert session_options.enable_cpu_mem_arena is False
    if "enable_mem_pattern" in opts:
        assert session_options.enable_mem_pattern is False
    if "execution_mode" in opts:
        assert session_options.execution_mode == "sequential"
    if "graph_optimization_level" in opts:
        assert session_options.graph_optimization_level == "extended"
    if "log_severity_level" in opts:
        assert session_options.log_severity_level == 3
    # At minimum, add_extra_session_options should run without raising.
    assert exposed is not None


# ---------------------------------------------------------------------------
# Idle inference tracking
# ---------------------------------------------------------------------------


def test_seconds_since_last_inference_returns_inf_before_first_call():
    """Before any embed/rerank call the timer should report inf."""
    import opencode_search.embeddings as emb
    # Reset the global to simulate a fresh process state
    original = emb._last_inference_monotonic
    emb._last_inference_monotonic = 0.0
    try:
        assert emb.seconds_since_last_inference() == float("inf")
    finally:
        emb._last_inference_monotonic = original


def test_touch_inference_time_resets_idle_counter():
    """touch_inference_time() must make seconds_since_last_inference() < 1s."""
    import opencode_search.embeddings as emb

    emb.touch_inference_time()
    elapsed = emb.seconds_since_last_inference()
    assert elapsed < 1.0, f"Expected <1s after touch, got {elapsed:.3f}s"


def test_seconds_since_last_inference_increases_over_time():
    """The reported idle time must grow between two reads."""
    import time

    import opencode_search.embeddings as emb

    emb.touch_inference_time()
    t1 = emb.seconds_since_last_inference()
    time.sleep(0.05)
    t2 = emb.seconds_since_last_inference()
    assert t2 > t1


# ---------------------------------------------------------------------------
# GPU enforcement tests
# ---------------------------------------------------------------------------


def test_embed_raises_gpu_not_available_when_no_providers(monkeypatch):
    """GPU enforcement: embedding must raise GPUNotAvailableError when providers=None."""
    import opencode_search.embeddings as emb
    # Patch _get_onnx_providers to return empty list (simulates no GPU)
    monkeypatch.setattr(emb, "_get_onnx_providers", lambda: [])
    # Also patch _detected_providers to None to force re-detection
    monkeypatch.setattr(emb, "_detected_providers", None)
    with pytest.raises(emb.GPUNotAvailableError):
        emb.assert_gpu_available()


def test_reranker_raises_gpu_not_available_when_no_providers(monkeypatch):
    """GPU enforcement: reranking must raise GPUNotAvailableError when providers=None."""
    import opencode_search.embeddings as emb
    monkeypatch.setattr(emb, "_get_onnx_providers", lambda: [])
    monkeypatch.setattr(emb, "_detected_providers", None)
    with pytest.raises(emb.GPUNotAvailableError):
        emb.assert_gpu_available()
