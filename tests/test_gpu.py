"""Each GPU assertion tested by removing what it guards against.

An assertion the broken path also satisfies is decoration. Every test here
forces the failure mode and asserts the refusal, not the happy state.
"""

from __future__ import annotations

import subprocess
import sys

import onnxruntime as ort
import pytest

from coderag import config, gpu


def test_ladder_offers_only_gpu_providers():
    assert set(gpu.ep_ladder()) <= set(gpu.GPU_EPS)
    assert "CPUExecutionProvider" not in gpu.ep_ladder()


def test_tensorrt_is_off_by_default_and_switchable(monkeypatch):
    if "TensorrtExecutionProvider" not in ort.get_available_providers():
        pytest.skip("no TensorRT EP installed to switch")
    monkeypatch.setattr(config, "DISABLE_TENSORRT", True)
    assert "TensorrtExecutionProvider" not in gpu.ep_ladder()
    monkeypatch.setattr(config, "DISABLE_TENSORRT", False)
    assert "TensorrtExecutionProvider" in gpu.ep_ladder()


def test_an_empty_ladder_raises_instead_of_falling_back(monkeypatch):
    """ORT's own behaviour here is to append CPU and run, so we must refuse."""
    monkeypatch.setattr(gpu, "ep_ladder", list)
    with pytest.raises(RuntimeError, match="CPU inference is forbidden"):
        gpu.providers()


def test_assert_gpu_available_exits_rather_than_raising():
    """A raise is swallowed by uvicorn's lifespan and leaves a live socket.

    Run out of process, because the thing under test is the exit code -- an
    in-process check could only observe SystemExit, which is not what systemd
    reads.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from coderag import gpu; gpu.ep_ladder = list; gpu.assert_gpu_available()",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 1
    assert "CPU inference is forbidden" in proc.stderr


@pytest.mark.parametrize("device", ["cpu", "CPU:0", "cuda:0,cpu", "CpU"])
def test_a_device_naming_cpu_is_rejected(device):
    with pytest.raises(ValueError, match="CPU inference is forbidden"):
        gpu.check_device(device)


def test_check_device_passes_a_gpu_device():
    assert gpu.check_device("cuda:0") == "cuda:0"


class _FakeSession:
    def __init__(self, providers):
        self._providers = providers

    def get_providers(self):
        return self._providers


@pytest.mark.parametrize("what", ["embedder", "reranker"])
def test_a_session_that_landed_on_cpu_is_rejected(what):
    """Checked for the reranker too: it is the larger model and the one that
    quietly landed on CPU back when only the embedder was verified."""
    session = _FakeSession(["CPUExecutionProvider"])
    with pytest.raises(RuntimeError, match="CPU inference is forbidden"):
        gpu.verify_session(session, what)


def test_a_session_reporting_nothing_is_rejected():
    with pytest.raises(RuntimeError, match="not a GPU provider"):
        gpu.verify_session(_FakeSession([]), "embedder")


def test_a_gpu_session_passes():
    gpu.verify_session(_FakeSession(["CUDAExecutionProvider", "CPUExecutionProvider"]), "embedder")


def test_an_explicit_batch_override_wins(monkeypatch):
    monkeypatch.setattr(config, "EMBED_BATCH", 7)
    assert gpu.adaptive_batch(per_item_bytes=10**9) == 7


def test_batch_falls_to_the_floor_when_vram_is_unreadable(monkeypatch):
    monkeypatch.setattr(config, "EMBED_BATCH", 0)
    monkeypatch.setattr(gpu, "free_vram_bytes", lambda: 0)
    assert gpu.adaptive_batch(per_item_bytes=1024, floor=8) == 8


def test_batch_never_budgets_more_than_half_of_free_vram(monkeypatch):
    """The arena grows during a run and never gives it back, so budgeting all
    of free VRAM guarantees an OOM on the second batch."""
    monkeypatch.setattr(config, "EMBED_BATCH", 0)
    monkeypatch.setattr(gpu, "free_vram_bytes", lambda: 1024 * 1024 * 1024)
    per_item = 8 * 1024 * 1024
    assert gpu.adaptive_batch(per_item, floor=1, ceiling=1024) == 64
