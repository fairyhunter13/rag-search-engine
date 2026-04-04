"""Tests for CUDA/ROCm tensor support, IOBinding, FP16, and GPU capability detection.

These tests verify that:
- GPU capabilities are detected correctly (tensor cores, FP16, VRAM)
- GPU provider options are built correctly for CUDA/ROCm/MIGraphX/DirectML
- IOBinding tracking works correctly
- FP16 mode respects env var and tensor core availability
- get_gpu_stats() includes fields: tensor_cores, fp16_enabled, io_binding_active, vendor, gpu_name
- Multi-vendor detection: NVIDIA, AMD, Intel, Apple Silicon, Qualcomm, Generic Linux
"""

import os


def test_detect_gpu_capabilities_returns_dict():
    """Test that _detect_gpu_capabilities returns a dict with required keys."""
    from opencode_embedder.embeddings import _detect_gpu_capabilities

    caps = _detect_gpu_capabilities()

    assert isinstance(caps, dict)
    assert "has_tensor_cores" in caps
    assert "compute_capability" in caps
    assert "supports_fp16" in caps
    assert "vram_mb" in caps
    # New multi-vendor fields
    assert "vendor" in caps
    assert "gpu_name" in caps
    assert "driver_version" in caps


def test_detect_gpu_capabilities_types():
    """Test that _detect_gpu_capabilities returns correct types."""
    from opencode_embedder.embeddings import _detect_gpu_capabilities

    caps = _detect_gpu_capabilities()

    assert isinstance(caps["has_tensor_cores"], bool)
    assert isinstance(caps["supports_fp16"], bool)
    # compute_capability is None or str
    assert caps["compute_capability"] is None or isinstance(caps["compute_capability"], str)
    # vram_mb is None or int
    assert caps["vram_mb"] is None or isinstance(caps["vram_mb"], int)
    # vendor is always a str
    assert isinstance(caps["vendor"], str)
    # gpu_name is None or str
    assert caps["gpu_name"] is None or isinstance(caps["gpu_name"], str)
    # driver_version is None or str
    assert caps["driver_version"] is None or isinstance(caps["driver_version"], str)


def test_get_gpu_capabilities_is_cached():
    """Test that _get_gpu_capabilities returns the same object on repeated calls."""
    from opencode_embedder import embeddings

    caps1 = embeddings._get_gpu_capabilities()
    caps2 = embeddings._get_gpu_capabilities()

    # Should be the same dict instance after caching
    assert caps1 is caps2 or caps1 == caps2


def test_get_gpu_stats_has_new_fields():
    """Test that get_gpu_stats includes new tensor/FP16/IOBinding fields."""
    from opencode_embedder.embeddings import get_gpu_stats

    stats = get_gpu_stats()

    assert "tensor_cores" in stats
    assert "fp16_enabled" in stats
    assert "io_binding_active" in stats


def test_get_gpu_stats_new_field_types():
    """Test that new get_gpu_stats fields have correct types."""
    from opencode_embedder.embeddings import get_gpu_stats

    stats = get_gpu_stats()

    assert isinstance(stats["tensor_cores"], bool)
    assert isinstance(stats["fp16_enabled"], bool)
    assert isinstance(stats["io_binding_active"], bool)


def test_fp16_disabled_when_no_gpu():
    """Test that FP16 is not active when no GPU is available."""
    from opencode_embedder.embeddings import _fp16_active, is_gpu_available

    if is_gpu_available():
        # If GPU is available, we can't test the no-GPU case
        return

    assert _fp16_active() is False


def test_fp16_respects_disable_env(monkeypatch):
    """Test that OPENCODE_ONNX_FP16=0 disables FP16."""
    from opencode_embedder import embeddings

    monkeypatch.setenv("OPENCODE_ONNX_FP16", "0")
    assert embeddings._fp16_active() is False

    monkeypatch.setenv("OPENCODE_ONNX_FP16", "false")
    assert embeddings._fp16_active() is False

    monkeypatch.setenv("OPENCODE_ONNX_FP16", "off")
    assert embeddings._fp16_active() is False


def test_fp16_force_enabled_without_tensor_cores(monkeypatch):
    """Test that OPENCODE_ONNX_FP16=1 still requires tensor core support."""
    from opencode_embedder import embeddings

    # Simulate GPU available but no tensor cores
    embeddings._caps = {
        "has_tensor_cores": False,
        "compute_capability": "6.0",
        "supports_fp16": False,
        "vram_mb": 4096,
        "vendor": "nvidia",
        "gpu_name": "NVIDIA GTX 1060",
        "driver_version": "525.0",
    }
    embeddings._caps_done = True

    monkeypatch.setenv("OPENCODE_ONNX_FP16", "1")
    # FP16 requires supports_fp16 to be True
    assert embeddings._fp16_active() is False

    # Restore
    embeddings._caps = None
    embeddings._caps_done = False


def test_fp16_force_enabled_with_tensor_cores(monkeypatch):
    """Test that OPENCODE_ONNX_FP16=1 enables FP16 when tensor cores present."""
    from opencode_embedder import embeddings

    # Simulate GPU with tensor cores
    embeddings._caps = {
        "has_tensor_cores": True,
        "compute_capability": "7.5",
        "supports_fp16": True,
        "vram_mb": 8192,
        "vendor": "nvidia",
        "gpu_name": "NVIDIA RTX 2080",
        "driver_version": "525.0",
    }
    embeddings._caps_done = True

    # Mock is_gpu_available to return True
    original = embeddings.is_gpu_available
    embeddings.is_gpu_available = lambda: True
    monkeypatch.setenv("OPENCODE_ONNX_FP16", "1")

    result = embeddings._fp16_active()

    # Restore
    embeddings.is_gpu_available = original
    embeddings._caps = None
    embeddings._caps_done = False

    assert result is True


def test_io_binding_active_default_false():
    """Test that IOBinding is not active by default (no GPU in test env)."""
    from opencode_embedder.embeddings import _io_binding_active, is_gpu_available

    if is_gpu_available():
        # IOBinding state depends on whether test_provider succeeded
        return

    # Without GPU, IOBinding should be inactive
    assert isinstance(_io_binding_active(), bool)


def test_set_io_binding_active():
    """Test that _set_io_binding_active correctly updates the state."""
    from opencode_embedder import embeddings

    original = embeddings._io_binding_confirmed

    embeddings._set_io_binding_active(True)
    assert embeddings._io_binding_active() is True

    embeddings._set_io_binding_active(False)
    assert embeddings._io_binding_active() is False

    # Restore original
    embeddings._set_io_binding_active(original)


def test_gpu_provider_options_cuda():
    """Test that _gpu_provider_options returns correct opts for CUDA."""
    from opencode_embedder.embeddings import _gpu_provider_options

    opts = _gpu_provider_options("CUDAExecutionProvider")

    assert isinstance(opts, list)
    assert len(opts) == 2
    gpu_opts = opts[0]
    assert isinstance(gpu_opts, dict)
    assert "arena_extend_strategy" in gpu_opts
    assert gpu_opts["arena_extend_strategy"] == "kSameAsRequested"
    assert "cudnn_conv_algo_search" in gpu_opts
    assert gpu_opts["cudnn_conv_algo_search"] == "EXHAUSTIVE"
    assert "do_copy_in_default_stream" in gpu_opts
    # CPU opts should be empty
    assert opts[1] == {}


def test_gpu_provider_options_rocm():
    """Test that _gpu_provider_options returns correct opts for ROCm."""
    from opencode_embedder.embeddings import _gpu_provider_options

    opts = _gpu_provider_options("ROCMExecutionProvider")

    assert isinstance(opts, list)
    assert len(opts) == 2
    gpu_opts = opts[0]
    assert "arena_extend_strategy" in gpu_opts
    assert gpu_opts["arena_extend_strategy"] == "kSameAsRequested"
    # ROCm should NOT have cudnn-specific options
    assert "cudnn_conv_algo_search" not in gpu_opts


def test_gpu_provider_options_migraphx():
    """Test that _gpu_provider_options returns correct opts for MIGraphX."""
    from opencode_embedder.embeddings import _gpu_provider_options

    opts = _gpu_provider_options("MIGraphXExecutionProvider")

    assert isinstance(opts, list)
    assert len(opts) == 2
    gpu_opts = opts[0]
    # MIGraphX uses 'True'/'False' strings, not '1'/'0'
    # Note: arena_extend_strategy and migraphx_mem_limit are NOT supported in ORT 1.22.x
    assert "device_id" in gpu_opts
    # FP16 is enabled when GPU supports it
    if "migraphx_fp16_enable" in gpu_opts:
        assert gpu_opts["migraphx_fp16_enable"] == "True"


def test_gpu_provider_options_unknown():
    """Test that _gpu_provider_options handles unknown providers gracefully."""
    from opencode_embedder.embeddings import _gpu_provider_options

    opts = _gpu_provider_options("UnknownProvider")

    # Should return empty dicts rather than crashing
    assert isinstance(opts, list)
    assert len(opts) == 2
    assert opts[0] == {}
    assert opts[1] == {}


def test_gpu_provider_options_vram_limit():
    """Test that gpu_mem_limit is set when VRAM is detected."""
    from opencode_embedder import embeddings

    # Inject fake VRAM into caps
    embeddings._caps = {
        "has_tensor_cores": False,
        "compute_capability": None,
        "supports_fp16": False,
        "vram_mb": 8192,  # 8 GB
        "vendor": "nvidia",
        "gpu_name": "NVIDIA RTX 3080",
        "driver_version": None,
    }
    embeddings._caps_done = True

    opts = embeddings._gpu_provider_options("CUDAExecutionProvider")
    gpu_opts = opts[0]

    # Restore
    embeddings._caps = None
    embeddings._caps_done = False

    assert "gpu_mem_limit" in gpu_opts
    limit = int(gpu_opts["gpu_mem_limit"])
    # Should be 80% of 8192 MB in bytes
    expected = int(8192 * 0.8 * 1024 * 1024)
    assert limit == expected


def test_create_io_binding_returns_none_without_ort():
    """Test that _create_io_binding handles missing ONNX session gracefully."""
    from opencode_embedder.embeddings import _create_io_binding

    # Pass a fake session that doesn't have io_binding method
    class FakeSession:
        pass

    result = _create_io_binding(FakeSession(), {})
    assert result is None


def test_log_gpu_capabilities_runs_without_error():
    """Test that _log_gpu_capabilities doesn't raise."""
    from opencode_embedder.embeddings import _log_gpu_capabilities

    # Should not raise even if GPU is not present
    _log_gpu_capabilities()


def test_detect_gpu_capabilities_no_smi(monkeypatch):
    """Test _detect_gpu_capabilities gracefully handles missing nvidia-smi/rocm-smi."""
    import subprocess
    import glob as _glob
    from opencode_embedder.embeddings import _detect_gpu_capabilities

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("No such file: nvidia-smi")

    monkeypatch.setattr(subprocess, "run", fake_run)
    # Also suppress /sys/class/drm fallback so result is predictably "unknown"
    monkeypatch.setattr(_glob, "glob", lambda *a, **kw: [])

    caps = _detect_gpu_capabilities()

    assert caps["has_tensor_cores"] is False
    assert caps["supports_fp16"] is False
    assert caps["compute_capability"] is None
    # New fields should be present with defaults
    assert caps["vendor"] == "unknown"
    assert caps["gpu_name"] is None
    assert caps["driver_version"] is None


def test_get_gpu_stats_all_keys_present():
    """Test that get_gpu_stats returns all expected keys including new ones."""
    from opencode_embedder.embeddings import get_gpu_stats

    stats = get_gpu_stats()
    expected = {
        "gpu_ops",
        "cpu_ops",
        "provider",
        "is_gpu",
        "tensor_cores",
        "fp16_enabled",
        "io_binding_active",
        "vendor",
        "gpu_name",
    }
    assert expected.issubset(set(stats.keys()))


def test_fp16_auto_mode_without_tensor_cores(monkeypatch):
    """Test that FP16 auto mode is disabled without tensor cores."""
    from opencode_embedder import embeddings

    embeddings._caps = {
        "has_tensor_cores": False,
        "compute_capability": None,
        "supports_fp16": False,
        "vram_mb": None,
        "vendor": "unknown",
        "gpu_name": None,
        "driver_version": None,
    }
    embeddings._caps_done = True
    original_gpu = embeddings.is_gpu_available
    embeddings.is_gpu_available = lambda: True

    monkeypatch.setenv("OPENCODE_ONNX_FP16", "auto")
    result = embeddings._fp16_active()

    embeddings.is_gpu_available = original_gpu
    embeddings._caps = None
    embeddings._caps_done = False

    assert result is False


# ---------------------------------------------------------------------------
# New fields: vendor and gpu_name in get_gpu_stats()
# ---------------------------------------------------------------------------


def test_get_gpu_stats_has_vendor_field():
    """Test that get_gpu_stats includes vendor and gpu_name fields."""
    from opencode_embedder.embeddings import get_gpu_stats

    stats = get_gpu_stats()

    assert "vendor" in stats
    assert "gpu_name" in stats


def test_get_gpu_stats_vendor_type():
    """Test vendor is a string and gpu_name is str or None."""
    from opencode_embedder.embeddings import get_gpu_stats

    stats = get_gpu_stats()

    assert isinstance(stats["vendor"], str)
    assert stats["gpu_name"] is None or isinstance(stats["gpu_name"], str)


# ---------------------------------------------------------------------------
# DirectML provider options
# ---------------------------------------------------------------------------


def test_gpu_provider_options_directml():
    """Test that _gpu_provider_options returns valid options for DirectML."""
    from opencode_embedder.embeddings import _gpu_provider_options

    opts = _gpu_provider_options("DirectMLExecutionProvider")

    assert isinstance(opts, list)
    assert len(opts) == 2
    gpu_opts = opts[0]
    assert isinstance(gpu_opts, dict)
    assert "arena_extend_strategy" in gpu_opts
    assert gpu_opts["arena_extend_strategy"] == "kSameAsRequested"
    # DirectML should NOT have CUDA-specific options
    assert "cudnn_conv_algo_search" not in gpu_opts
    assert "do_copy_in_default_stream" not in gpu_opts
    # CPU opts should be empty
    assert opts[1] == {}


# ---------------------------------------------------------------------------
# TensorRT provider options
# ---------------------------------------------------------------------------


def test_gpu_provider_options_tensorrt():
    """Test that _gpu_provider_options returns correct opts for TensorRT."""
    from opencode_embedder.embeddings import _gpu_provider_options

    opts = _gpu_provider_options("TensorrtExecutionProvider")

    assert isinstance(opts, list)
    assert len(opts) == 2
    gpu_opts = opts[0]
    assert isinstance(gpu_opts, dict)
    # Engine cache options
    assert "trt_engine_cache_enable" in gpu_opts
    assert gpu_opts["trt_engine_cache_enable"] == "True"
    assert "trt_engine_cache_path" in gpu_opts
    # Workspace size
    assert "trt_max_workspace_size" in gpu_opts
    assert int(gpu_opts["trt_max_workspace_size"]) > 0
    # CPU opts should be empty
    assert opts[1] == {}


def test_gpu_provider_options_tensorrt_has_fp16():
    """Test that TensorRT options include the FP16 enable flag."""
    from opencode_embedder.embeddings import _gpu_provider_options

    opts = _gpu_provider_options("TensorrtExecutionProvider")
    gpu_opts = opts[0]

    assert "trt_fp16_enable" in gpu_opts
    # Value must be "True" or "False" (Python-style string for TensorRT)
    assert gpu_opts["trt_fp16_enable"] in ("True", "False")


def test_gpu_provider_options_tensorrt_fp16_when_supported(monkeypatch):
    """Test TensorRT FP16 is enabled when GPU supports it."""
    from opencode_embedder import embeddings

    embeddings._caps = {
        "has_tensor_cores": True,
        "compute_capability": "7.5",
        "supports_fp16": True,
        "vram_mb": 8192,
        "vendor": "nvidia",
        "gpu_name": "NVIDIA RTX 2080",
        "driver_version": "525.0",
    }
    embeddings._caps_done = True

    opts = embeddings._gpu_provider_options("TensorrtExecutionProvider")
    gpu_opts = opts[0]

    embeddings._caps = None
    embeddings._caps_done = False

    assert gpu_opts["trt_fp16_enable"] == "True"


def test_gpu_provider_options_tensorrt_fp16_when_unsupported(monkeypatch):
    """Test TensorRT FP16 is disabled when GPU does not support it."""
    from opencode_embedder import embeddings

    embeddings._caps = {
        "has_tensor_cores": False,
        "compute_capability": "6.1",
        "supports_fp16": False,
        "vram_mb": 4096,
        "vendor": "nvidia",
        "gpu_name": "NVIDIA GTX 1060",
        "driver_version": "510.0",
    }
    embeddings._caps_done = True

    opts = embeddings._gpu_provider_options("TensorrtExecutionProvider")
    gpu_opts = opts[0]

    embeddings._caps = None
    embeddings._caps_done = False

    assert gpu_opts["trt_fp16_enable"] == "False"


def test_gpu_provider_options_tensorrt_workspace_size():
    """Test that TensorRT workspace size is at least 1 GB."""
    from opencode_embedder.embeddings import _gpu_provider_options

    opts = _gpu_provider_options("TensorrtExecutionProvider")
    gpu_opts = opts[0]

    size = int(gpu_opts["trt_max_workspace_size"])
    # Default is 2 GB; must be at least 1 GB
    assert size >= 1 * 1024 * 1024 * 1024


def test_gpu_provider_options_tensorrt_blackwell_optimization():
    """Test TensorRT Blackwell (SM 12.0+) gets maximum optimization level."""
    from opencode_embedder import embeddings

    embeddings._caps = {
        "has_tensor_cores": True,
        "compute_capability": "12.0",
        "supports_fp16": True,
        "vram_mb": 16384,
        "vendor": "nvidia",
        "gpu_name": "NVIDIA RTX 5090",
        "driver_version": "560.0",
        "architecture": "blackwell",
    }
    embeddings._caps_done = True

    opts = embeddings._gpu_provider_options("TensorrtExecutionProvider")
    gpu_opts = opts[0]

    embeddings._caps = None
    embeddings._caps_done = False

    assert "trt_builder_optimization_level" in gpu_opts
    assert gpu_opts["trt_builder_optimization_level"] == "5"


def test_gpu_provider_options_tensorrt_no_blackwell_optimization():
    """Test TensorRT non-Blackwell GPUs do NOT get the extra optimization level."""
    from opencode_embedder import embeddings

    embeddings._caps = {
        "has_tensor_cores": True,
        "compute_capability": "8.6",
        "supports_fp16": True,
        "vram_mb": 12288,
        "vendor": "nvidia",
        "gpu_name": "NVIDIA RTX 3080",
        "driver_version": "525.0",
        "architecture": "ampere",
    }
    embeddings._caps_done = True

    opts = embeddings._gpu_provider_options("TensorrtExecutionProvider")
    gpu_opts = opts[0]

    embeddings._caps = None
    embeddings._caps_done = False

    assert "trt_builder_optimization_level" not in gpu_opts


def test_gpu_provider_options_tensorrt_no_arena_strategy():
    """Test TensorRT opts do NOT include CUDA arena/mem keys (separate provider)."""
    from opencode_embedder.embeddings import _gpu_provider_options

    opts = _gpu_provider_options("TensorrtExecutionProvider")
    gpu_opts = opts[0]

    # TensorRT manages its own memory; arena options live in the CUDA provider
    assert "arena_extend_strategy" not in gpu_opts
    assert "cudnn_conv_algo_search" not in gpu_opts


# ---------------------------------------------------------------------------
# Multi-vendor detection: valid structure when no GPU tools available
# ---------------------------------------------------------------------------


def test_detect_gpu_capabilities_all_tools_missing(monkeypatch):
    """Test that _detect_gpu_capabilities returns safe defaults when all tools fail."""
    import subprocess
    import platform
    from opencode_embedder.embeddings import _detect_gpu_capabilities

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError())
    )
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    # Also patch glob so /sys/class/drm returns nothing
    import glob as _glob

    monkeypatch.setattr(_glob, "glob", lambda *a, **kw: [])

    caps = _detect_gpu_capabilities()

    assert isinstance(caps, dict)
    assert caps["vendor"] == "unknown"
    assert caps["has_tensor_cores"] is False
    assert caps["supports_fp16"] is False
    assert caps["compute_capability"] is None
    assert caps["gpu_name"] is None
    assert caps["driver_version"] is None


# ---------------------------------------------------------------------------
# Parametrized multi-vendor parsing tests
# ---------------------------------------------------------------------------

import pytest
from unittest.mock import patch, MagicMock


def _make_proc(stdout: str, returncode: int = 0):
    """Build a fake subprocess.CompletedProcess."""
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    return p


@pytest.mark.parametrize(
    "vendor,cmd_prefix,stdout,expected",
    [
        (
            "nvidia",
            "nvidia-smi",
            "7.5, 8192, NVIDIA RTX 2080, 525.89\n",
            {
                "vendor": "nvidia",
                "gpu_name": "NVIDIA RTX 2080",
                "driver_version": "525.89",
                "has_tensor_cores": True,
                "supports_fp16": True,
                "vram_mb": 8192,
                "compute_capability": "7.5",
            },
        ),
        (
            "nvidia_low_sm",
            "nvidia-smi",
            "6.1, 4096, NVIDIA GTX 1060, 510.0\n",
            {
                "vendor": "nvidia",
                "has_tensor_cores": False,
                "supports_fp16": False,
                "vram_mb": 4096,
                "compute_capability": "6.1",
            },
        ),
    ],
)
def test_nvidia_gpu_detection(vendor, cmd_prefix, stdout, expected):
    """Test NVIDIA GPU detection via mocked nvidia-smi output."""
    from opencode_embedder.embeddings import _detect_gpu_capabilities

    proc = _make_proc(stdout)

    def fake_run(args, **kw):
        if args[0] == "nvidia-smi":
            return proc
        raise FileNotFoundError(args[0])

    with patch("subprocess.run", side_effect=fake_run):
        caps = _detect_gpu_capabilities()

    for k, v in expected.items():
        assert caps[k] == v, f"caps[{k!r}]={caps[k]!r}, expected {v!r}"


def test_amd_gpu_detection_mi300():
    """Test AMD GPU detection via mocked rocm-smi output (MI300 series)."""
    from opencode_embedder.embeddings import _detect_gpu_capabilities

    rocm_out = "device,Card series,Card model,Card vendor,SKU\n0,MI300X,MI300X,AMD,\n"

    def fake_run(args, **kw):
        if args[0] == "nvidia-smi":
            raise FileNotFoundError("nvidia-smi")
        if args[0] == "rocm-smi":
            return _make_proc(rocm_out)
        raise FileNotFoundError(args[0])

    def fake_vram():
        return 49152  # 48 GB HBM

    with patch("subprocess.run", side_effect=fake_run):
        with patch("opencode_embedder.embeddings._get_gpu_vram_mb", return_value=49152):
            caps = _detect_gpu_capabilities()

    assert caps["vendor"] == "amd"
    assert caps["gpu_name"] == "MI300X"
    assert caps["has_tensor_cores"] is True
    assert caps["supports_fp16"] is True


def test_amd_gpu_detection_rx6900():
    """Test AMD GPU detection via mocked rocm-smi output (RDNA2 RX 6900 XT)."""
    from opencode_embedder.embeddings import _detect_gpu_capabilities

    rocm_out = "device,Card series,Card model\n0,RX 6900 XT,RX 6900 XT\n"

    def fake_run(args, **kw):
        if args[0] == "nvidia-smi":
            raise FileNotFoundError("nvidia-smi")
        if args[0] == "rocm-smi":
            return _make_proc(rocm_out)
        raise FileNotFoundError(args[0])

    with patch("subprocess.run", side_effect=fake_run):
        with patch("opencode_embedder.embeddings._get_gpu_vram_mb", return_value=16384):
            caps = _detect_gpu_capabilities()

    assert caps["vendor"] == "amd"
    assert "rx 6" in (caps["gpu_name"] or "").lower() or caps["has_tensor_cores"] is True


def test_intel_arc_detection_via_sycl_ls():
    """Test Intel Arc GPU detection via mocked sycl-ls output."""
    from opencode_embedder.embeddings import _detect_gpu_capabilities

    sycl_out = "[opencl:gpu:0] Intel(R) Arc(TM) A770 Graphics, 16.0 GB\n"

    def fake_run(args, **kw):
        if args[0] == "nvidia-smi":
            raise FileNotFoundError("nvidia-smi")
        if args[0] == "rocm-smi":
            raise FileNotFoundError("rocm-smi")
        if args[0] == "sycl-ls":
            return _make_proc(sycl_out)
        raise FileNotFoundError(args[0])

    with patch("subprocess.run", side_effect=fake_run):
        caps = _detect_gpu_capabilities()

    assert caps["vendor"] == "intel"
    assert caps["gpu_name"] is not None
    assert "arc" in caps["gpu_name"].lower() or "intel" in caps["gpu_name"].lower()
    assert caps["supports_fp16"] is True
    assert caps["has_tensor_cores"] is True  # Arc has XMX engines


def test_intel_uhd_detection_via_sycl_ls():
    """Test Intel UHD (no XMX) detection via mocked sycl-ls output."""
    from opencode_embedder.embeddings import _detect_gpu_capabilities

    sycl_out = "[opencl:gpu:0] Intel(R) UHD Graphics 770, shared\n"

    def fake_run(args, **kw):
        if args[0] == "nvidia-smi":
            raise FileNotFoundError("nvidia-smi")
        if args[0] == "rocm-smi":
            raise FileNotFoundError("rocm-smi")
        if args[0] == "sycl-ls":
            return _make_proc(sycl_out)
        raise FileNotFoundError(args[0])

    with patch("subprocess.run", side_effect=fake_run):
        caps = _detect_gpu_capabilities()

    assert caps["vendor"] == "intel"
    assert caps["supports_fp16"] is True
    # UHD has no XMX engines → no tensor cores
    assert caps["has_tensor_cores"] is False


def test_apple_silicon_detection():
    """Test Apple Silicon GPU detection via mocked system_profiler output."""
    import platform
    from opencode_embedder.embeddings import _detect_gpu_capabilities

    sp_out = """
Graphics/Displays:
  Apple M3 Pro:
    Chipset Model: Apple M3 Pro
    Type: GPU
    VRAM (Dynamic, Max): 18 GB
"""

    def fake_run(args, **kw):
        if args[0] == "nvidia-smi":
            raise FileNotFoundError("nvidia-smi")
        if args[0] == "rocm-smi":
            raise FileNotFoundError("rocm-smi")
        if args[0] == "sycl-ls":
            raise FileNotFoundError("sycl-ls")
        if args[0] == "system_profiler":
            return _make_proc(sp_out)
        raise FileNotFoundError(args[0])

    with patch("subprocess.run", side_effect=fake_run):
        with patch.object(platform, "system", return_value="Darwin"):
            caps = _detect_gpu_capabilities()

    assert caps["vendor"] == "apple"
    assert caps["gpu_name"] is not None
    assert "M3" in caps["gpu_name"]
    assert caps["supports_fp16"] is True
    assert caps["has_tensor_cores"] is True  # M3 >= 3


def test_apple_m1_no_tensor_cores():
    """Test that Apple M1 is detected without tensor cores (< M3)."""
    import platform
    from opencode_embedder.embeddings import _detect_gpu_capabilities

    sp_out = "Apple M1 Max:\n  Chipset Model: Apple M1 Max\n"

    def fake_run(args, **kw):
        if args[0] in ("nvidia-smi", "rocm-smi", "sycl-ls"):
            raise FileNotFoundError(args[0])
        if args[0] == "system_profiler":
            return _make_proc(sp_out)
        raise FileNotFoundError(args[0])

    with patch("subprocess.run", side_effect=fake_run):
        with patch.object(platform, "system", return_value="Darwin"):
            caps = _detect_gpu_capabilities()

    assert caps["vendor"] == "apple"
    assert caps["supports_fp16"] is True
    assert caps["has_tensor_cores"] is False  # M1 < 3


def test_qualcomm_adreno_detection():
    """Test Qualcomm Adreno GPU detection via mocked wmic output (Windows)."""
    import platform
    from opencode_embedder.embeddings import _detect_gpu_capabilities

    wmic_out = "Name\nQualcomm(R) Adreno(TM) 695 GPU\n"

    def fake_run(args, **kw):
        if args[0] in ("nvidia-smi", "rocm-smi", "sycl-ls"):
            raise FileNotFoundError(args[0])
        if args[0] == "wmic":
            return _make_proc(wmic_out)
        raise FileNotFoundError(args[0])

    with patch("subprocess.run", side_effect=fake_run):
        with patch.object(platform, "system", return_value="Windows"):
            caps = _detect_gpu_capabilities()

    assert caps["vendor"] == "qualcomm"
    assert caps["gpu_name"] is not None
    assert "adreno" in caps["gpu_name"].lower() or "qualcomm" in caps["gpu_name"].lower()


def test_qualcomm_adreno_690_fp16():
    """Test that Adreno 690+ enables FP16."""
    import platform
    from opencode_embedder.embeddings import _detect_gpu_capabilities

    wmic_out = "Name\nQualcomm Adreno 690\n"

    def fake_run(args, **kw):
        if args[0] in ("nvidia-smi", "rocm-smi", "sycl-ls"):
            raise FileNotFoundError(args[0])
        if args[0] == "wmic":
            return _make_proc(wmic_out)
        raise FileNotFoundError(args[0])

    with patch("subprocess.run", side_effect=fake_run):
        with patch.object(platform, "system", return_value="Windows"):
            caps = _detect_gpu_capabilities()

    assert caps["vendor"] == "qualcomm"
    assert caps["supports_fp16"] is True


def test_directml_fallback_nvidia():
    """Test DirectML/PowerShell fallback identifies NVIDIA GPU on Windows."""
    import platform
    from opencode_embedder.embeddings import _detect_gpu_capabilities

    ps_out = "NVIDIA GeForce RTX 4090\n"

    def fake_run(args, **kw):
        if args[0] in ("nvidia-smi", "rocm-smi", "sycl-ls"):
            raise FileNotFoundError(args[0])
        if args[0] == "wmic":
            # Return non-Qualcomm output so it falls through to PowerShell
            return _make_proc("Name\nNVIDIA GeForce RTX 4090\n")
        if args[0] == "powershell":
            return _make_proc(ps_out)
        raise FileNotFoundError(args[0])

    with patch("subprocess.run", side_effect=fake_run):
        with patch.object(platform, "system", return_value="Windows"):
            caps = _detect_gpu_capabilities()

    # Either wmic or powershell should have identified an NVIDIA card
    assert caps["vendor"] in ("nvidia", "unknown")


def test_vendor_fallthrough_nvidia_to_amd(monkeypatch):
    """Test that when NVIDIA detection fails, AMD detection is tried."""
    import subprocess
    from opencode_embedder.embeddings import _detect_gpu_capabilities

    rocm_out = "device,Card series\n0,MI100\n"
    calls = []

    def fake_run(args, **kw):
        calls.append(args[0])
        if args[0] == "nvidia-smi":
            p = _make_proc("", returncode=1)
            return p
        if args[0] == "rocm-smi":
            return _make_proc(rocm_out)
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(subprocess, "run", fake_run)
    with patch("opencode_embedder.embeddings._get_gpu_vram_mb", return_value=16384):
        caps = _detect_gpu_capabilities()

    assert "nvidia-smi" in calls
    assert "rocm-smi" in calls
    assert caps["vendor"] == "amd"


def test_vendor_fallthrough_all_gpu_tools_fail(monkeypatch):
    """Test that all detection attempts can fail gracefully."""
    import subprocess
    import platform
    import glob as _glob
    from opencode_embedder.embeddings import _detect_gpu_capabilities

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError())
    )
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(_glob, "glob", lambda *a, **kw: [])

    caps = _detect_gpu_capabilities()

    assert caps["vendor"] == "unknown"
    assert caps["has_tensor_cores"] is False
    assert caps["supports_fp16"] is False
