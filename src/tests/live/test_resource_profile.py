"""Resource profile assertions: GPU resident, no orphan browsers, LLM throughput floor.

These tests verify that the test-run resource optimisations are in effect (Part D of
Phase 69) and that the production daemon uses the GPU, not the CPU.
"""
from __future__ import annotations

import os
import time

import httpx
import pytest

pytestmark = pytest.mark.live

_OLLAMA_PS_URL = os.environ.get("OPENCODE_OLLAMA_PS_URL", "http://localhost:11434/api/ps")
_OLLAMA_GEN_URL = "http://localhost:11434/api/generate"


@pytest.fixture(scope="module", autouse=True)
def _warmup_qwen3_query():
    """Ensure qwen3-query:8b is loaded and GPU-resident before residency tests run.

    Uses keep_alive=-1 to pin the model, then polls /api/ps until size_vram > 0
    to avoid a race where VRAM allocation is still in progress when the test checks.
    """
    try:
        httpx.post(
            _OLLAMA_GEN_URL,
            json={"model": "qwen3-query:8b", "prompt": "ok", "stream": False,
                  "options": {"num_predict": 1}, "keep_alive": "-1"},
            timeout=90.0,
        )
    except Exception:
        return  # Ollama unreachable — test will fail with a clear error

    # Poll until the model reports VRAM > 0 (GPU allocation may lag the generate call)
    for _ in range(15):
        try:
            r = httpx.get(_OLLAMA_PS_URL, timeout=5.0)
            models = r.json().get("models", [])
            for m in models:
                if "qwen3-query" in m.get("name", "").lower() and m.get("size_vram", 0) > 0:
                    return  # GPU-resident, proceed
        except Exception:
            pass
        time.sleep(2)


def test_ollama_qwen3_query_is_gpu_resident():
    """qwen3-query:8b must be loaded with VRAM > 0 (GPU, not CPU-only)."""
    try:
        r = httpx.get(_OLLAMA_PS_URL, timeout=5.0)
    except Exception as exc:
        pytest.fail(f"Ollama not reachable at {_OLLAMA_PS_URL}: {exc}")
    assert r.status_code == 200, f"Unexpected status from /api/ps: {r.status_code}"
    models = r.json().get("models", [])
    query_models = [m for m in models if "qwen3-query" in m.get("name", "").lower()
                    or "qwen3-query" in m.get("model", "").lower()]
    assert query_models, "qwen3-query:8b is not currently loaded — _pin_ollama_model_resident should keep it loaded"
    for m in query_models:
        size_vram = m.get("size_vram", 0)
        assert size_vram > 0, (
            f"qwen3-query model has size_vram={size_vram} — running on CPU, not GPU"
        )


def test_ollama_query_llm_throughput_floor():
    """A short prompt must complete under a generous ceiling (GPU is ~10× faster than CPU)."""
    try:
        start = time.monotonic()
        r = httpx.post(
            _OLLAMA_GEN_URL,
            json={"model": "qwen3-query:8b", "prompt": "Say only: ok", "stream": False,
                  "options": {"num_predict": 4}},
            timeout=60.0,
        )
        elapsed = time.monotonic() - start
    except Exception as exc:
        pytest.fail(f"Ollama not reachable: {exc}")
    assert r.status_code == 200, f"Unexpected status from /api/generate: {r.status_code}"
    data = r.json()
    eval_count = data.get("eval_count") or data.get("prompt_eval_count") or 1
    eval_ns = data.get("eval_duration", 0)
    if eval_ns > 0:
        ms_per_token = (eval_ns / 1e6) / eval_count
        assert ms_per_token < 4000, (
            f"LLM eval at {ms_per_token:.0f}ms/token — expected <4000ms/token on GPU "
            f"(CPU fallback would be 5000ms+/token; GPU under concurrent load may reach ~3000ms/token)"
        )
    else:
        assert elapsed < 30.0, f"Query LLM took {elapsed:.1f}s — unexpectedly slow (CPU fallback?)"


def test_args_fixture_decorator_remains_session_scoped():
    """Code-audit guard: browser_type_launch_args must declare scope='session' in conftest.py.
    Catches accidental scope demotion; see test_browser_is_session_scoped_* for the
    behavioural assertion that the browser process is actually shared."""
    import ast
    import pathlib

    conftest = pathlib.Path(__file__).parent / "conftest.py"
    src = conftest.read_text()
    tree = ast.parse(src)
    session_fixtures = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call):
                    for kw in dec.keywords:
                        if (kw.arg == "scope" and isinstance(kw.value, ast.Constant)
                                and kw.value.value == "session"):
                            session_fixtures.add(node.name)
    assert "browser_type_launch_args" in session_fixtures, (
        "browser_type_launch_args fixture must be scope='session' in conftest.py "
        "(D1 — one Chromium launch per test run)"
    )


