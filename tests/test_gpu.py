"""Each GPU assertion tested by removing what it guards against.

An assertion the broken path also satisfies is decoration. Every test here
forces the failure mode and asserts the refusal, not the happy state.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import inspect
import subprocess
import sys

import onnxruntime as ort
import pytest

from coderag import config, gpu, tools


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


def test_no_caller_can_name_a_device():
    """`check_device` was deleted because nothing took a device; this replaces it.

    A guard with no call site is worse than no guard -- it read as one of four
    assertions while never running. If a device parameter is ever introduced,
    this fails and the guard has to come back with it.
    """
    named = [k for k in vars(config) if not k.startswith("_") and "DEVICE" in k]
    assert named == [], f"config now names a device ({named}); re-introduce the device guard"
    for tool in (tools.search_code, tools.index_project):
        params = set(inspect.signature(tool).parameters)
        assert not params & {"device", "provider", "ep"}, f"{tool.__name__} takes a device"


def test_the_installed_runtime_is_the_gpu_wheel():
    """The CPU wheel exports a CUDAExecutionProvider name that resolves to CPU.

    `pyproject.toml` argues this in a comment and nothing checked it against the
    environment actually running, which is where a stray `pip install
    onnxruntime` lands.
    """
    installed = {d.metadata["Name"] for d in importlib.metadata.distributions()}
    assert "onnxruntime-gpu" in installed
    assert "onnxruntime" not in installed, "the CPU wheel is installed and will shadow the GPU one"


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


# (op_type, provider, duration_us) -- the shape an ORT profile gives us.
CUDA = "CUDAExecutionProvider"
CPU = "CPUExecutionProvider"
# The real measurement, both exports, 2026-08-20: nine shape nodes, 186 us of
# 317,519. Everything below is that census with one thing changed.
MEASURED = [("MatMul", CUDA, 317_333), ("Gather", CPU, 75), ("Where", CPU, 27)] + [
    (op, CPU, 21) for op in ("Unsqueeze", "Unsqueeze", "Concat", "Equal", "Gather", "Gather")
]


def test_the_measured_placement_passes():
    """The control. A check that refuses reality refuses everything after it."""
    gpu.check_placement(MEASURED, "embedder")


def test_tensor_math_on_the_cpu_is_rejected():
    """The failure the EP list cannot see: CUDA first, and a MatMul on CPU anyway."""
    with pytest.raises(RuntimeError, match="CPU inference is forbidden"):
        gpu.check_placement([*MEASURED, ("MatMul", CPU, 12)], "embedder")


def test_an_allowlisted_op_doing_real_work_is_still_rejected():
    """`Gather` is on the allowlist and `Gather` is also an embedding lookup.

    This is the case the op set alone cannot catch, and the reason there is a
    time bound at all: same op name, four orders of magnitude more work.
    """
    with pytest.raises(RuntimeError, match="of node time off the GPU"):
        gpu.check_placement([("MatMul", CUDA, 1000), ("Gather", CPU, 500)], "embedder")


def test_a_profile_with_no_gpu_node_is_rejected():
    """Otherwise an empty or all-CPU profile passes every other clause vacuously."""
    with pytest.raises(RuntimeError, match="no GPU node at all"):
        gpu.check_placement([("Gather", CPU, 5)], "embedder")


def test_the_allowlist_cannot_be_widened_into_uselessness():
    """Guards the fix-by-allowlisting move: the cheapest way to green a red
    placement test is to add the offending op, and these are the ops that would
    mean the model is running on the CPU."""
    weighted = {"MatMul", "Gemm", "Conv", "Attention", "LayerNormalization", "Softmax", "Erf"}
    assert gpu.SHAPE_ONLY_OPS.isdisjoint(weighted)


def test_serve_refuses_before_it_reaches_the_socket(monkeypatch):
    """Not just that it exits, but that it exits *first*.

    A gate inside the lifespan also exits non-zero, and leaves a bound port
    answering while it does. Checking the port afterwards cannot tell those
    apart -- the process is gone either way -- so this asserts `server.serve`
    was never reached.
    """
    from coderag import cli, server

    monkeypatch.setattr(gpu, "ep_ladder", list)
    monkeypatch.setattr(server, "serve", lambda *_a, **_k: pytest.fail("bound the socket anyway"))
    with pytest.raises(SystemExit) as exc:
        cli._serve(argparse.Namespace(host="", port=0))
    assert exc.value.code == 1
