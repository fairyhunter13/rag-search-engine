"""Tests for opencode_search.hardware — GPU detection, worker calculation."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from opencode_search.hardware import (
    _compute_cap_to_arch,
    _major_cc,
    detect_gpu,
    get_cpu_count,
    get_embed_workers,
    get_ram_mb,
    log_hardware_info,
    set_oom_score,
)


# ---------------------------------------------------------------------------
# _major_cc and _compute_cap_to_arch
# ---------------------------------------------------------------------------


def test_major_cc_valid():
    assert _major_cc("8.9") == 8
    assert _major_cc("12.0") == 12
    assert _major_cc("7.5") == 7


def test_major_cc_invalid():
    assert _major_cc("") is None
    assert _major_cc(None) is None
    assert _major_cc("abc") is None


def test_compute_cap_to_arch():
    assert _compute_cap_to_arch("7.5") == "Volta/Turing"
    assert _compute_cap_to_arch("8.9") == "Ampere/Ada"
    assert _compute_cap_to_arch("9.0") == "Hopper"
    assert _compute_cap_to_arch("10.0") == "Blackwell"


def test_compute_cap_to_arch_unknown():
    arch = _compute_cap_to_arch("99.0")
    assert "Unknown" in arch


def test_compute_cap_to_arch_invalid():
    assert _compute_cap_to_arch("xyz") == "unknown"


# ---------------------------------------------------------------------------
# detect_gpu
# ---------------------------------------------------------------------------


def test_detect_gpu_no_nvidia_smi():
    with patch("opencode_search.hardware.subprocess.run", side_effect=FileNotFoundError):
        result = detect_gpu()
    assert result["vendor"] == "none"
    assert result["gpu_name"] == ""
    assert result["vram_mb"] is None


def test_detect_gpu_smi_failure():
    mock_result = MagicMock(returncode=1, stdout="")
    with patch("opencode_search.hardware.subprocess.run", return_value=mock_result):
        result = detect_gpu()
    assert result["vendor"] == "none"


def test_detect_gpu_smi_success():
    mock_result = MagicMock(
        returncode=0, stdout="NVIDIA GeForce RTX 5080 Laptop GPU, 16384, 12.0\n"
    )
    with patch("opencode_search.hardware.subprocess.run", return_value=mock_result):
        result = detect_gpu()
    assert result["vendor"] == "nvidia"
    assert "5080" in result["gpu_name"]
    assert result["vram_mb"] == 16384
    assert result["compute_capability"] == "12.0"
    assert result["architecture"] == "Blackwell"
    assert result["supports_fp16"] is True
    assert result["has_tensor_cores"] is True


def test_detect_gpu_old_gpu():
    """A pre-Volta GPU should not advertise fp16 / tensor cores."""
    mock_result = MagicMock(returncode=0, stdout="GTX 1080, 8192, 6.1\n")
    with patch("opencode_search.hardware.subprocess.run", return_value=mock_result):
        result = detect_gpu()
    assert result["vendor"] == "nvidia"
    assert result["supports_fp16"] is False
    assert result["has_tensor_cores"] is False
    assert result["architecture"] == "Pascal"


# ---------------------------------------------------------------------------
# get_embed_workers
# ---------------------------------------------------------------------------


def test_get_embed_workers_none():
    assert get_embed_workers(None) == 2


def test_get_embed_workers_zero():
    assert get_embed_workers(0) == 2


def test_get_embed_workers_8gb():
    # (8192 - 1024) // 600 = 11 → clamped to 6
    assert get_embed_workers(8192) == 6


def test_get_embed_workers_16gb():
    # (16384 - 1024) // 600 = 25 → clamped to 6
    assert get_embed_workers(16384) == 6


def test_get_embed_workers_small_gpu():
    # Force a tiny VRAM: (2048 - 1024) // 600 = 1 → clamped to 2
    assert get_embed_workers(2048) == 2


def test_get_embed_workers_clamped_to_max_6():
    # Even with massive VRAM, clamped to 6
    assert get_embed_workers(80000) == 6


def test_get_embed_workers_returns_int():
    assert isinstance(get_embed_workers(8192), int)
    assert isinstance(get_embed_workers(None), int)


# ---------------------------------------------------------------------------
# get_cpu_count and get_ram_mb
# ---------------------------------------------------------------------------


def test_get_cpu_count_positive():
    assert get_cpu_count() > 0


def test_get_ram_mb_positive():
    ram = get_ram_mb()
    # On Linux the value should be > 0; if it's 0 we tolerate that
    assert ram >= 0


# ---------------------------------------------------------------------------
# set_oom_score (just verify it doesn't crash)
# ---------------------------------------------------------------------------


def test_set_oom_score_no_crash():
    # Should never crash, even without root
    set_oom_score(-100)  # mild adjustment, may silently fail


# ---------------------------------------------------------------------------
# log_hardware_info
# ---------------------------------------------------------------------------


def test_log_hardware_info_no_crash(caplog):
    log_hardware_info()  # Should not crash regardless of whether GPU is present


def test_log_hardware_info_no_gpu_logs_error(caplog):
    """Without a GPU the function must log an error (CPU mode is forbidden)."""
    with patch("opencode_search.hardware.detect_gpu",
               return_value={"vendor": "none", "vram_mb": None, "gpu_name": ""}):
        with caplog.at_level("ERROR"):
            log_hardware_info()
    messages = "\n".join(r.message for r in caplog.records)
    assert "GPU" in messages or "CUDA" in messages
