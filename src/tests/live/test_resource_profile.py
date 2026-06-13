"""Resource profile assertions: GPU resident, no orphan browsers, LLM throughput floor.

These tests verify that the test-run resource optimisations are in effect and that
the production daemon uses the GPU, not the CPU.

U3: single resident model is qwen3-enrich:1.7b on :11434 (qwen3-query:8b retired).
U6: test_no_8b_model_resident guards that 8b is NOT kept resident.
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
def _warmup_enrich_model():
    """Ensure qwen3-enrich:1.7b is loaded and GPU-resident before residency tests run.

    U3: qwen3-enrich:1.7b is the single resident model (8b retired).
    Uses keep_alive=-1 to pin the model, then polls /api/ps until size_vram > 0
    to avoid a race where VRAM allocation is still in progress when the test checks.
    """
    try:
        httpx.post(
            _OLLAMA_GEN_URL,
            json={"model": "qwen3-enrich:1.7b", "prompt": "ok", "stream": False,
                  "options": {"num_predict": 1}, "keep_alive": -1},
            timeout=90.0,
        )
    except Exception:
        return  # Ollama unreachable — test will fail with a clear error

    # Poll until the model reports VRAM > 0 (GPU allocation may lag the generate call).
    for _ in range(30):
        try:
            r = httpx.get(_OLLAMA_PS_URL, timeout=5.0)
            models = r.json().get("models", [])
            for m in models:
                if "qwen3-enrich" in m.get("name", "").lower() and m.get("size_vram", 0) > 0:
                    return  # GPU-resident, proceed
        except Exception:
            pass
        time.sleep(2)


def test_ollama_enrich_model_is_gpu_resident():
    """qwen3-enrich:1.7b must be loaded with VRAM > 0 (GPU, not CPU-only).

    U3: the single resident model is qwen3-enrich:1.7b on :11434.
    Re-triggers a load immediately before checking so the test is self-contained
    and not susceptible to model eviction during the test run.
    """
    try:
        httpx.post(
            _OLLAMA_GEN_URL,
            json={"model": "qwen3-enrich:1.7b", "prompt": "ok", "stream": False,
                  "options": {"num_predict": 1}, "keep_alive": -1},
            timeout=90.0,
        )
    except Exception as exc:
        pytest.fail(f"Ollama not reachable at {_OLLAMA_GEN_URL}: {exc}")

    # Poll until GPU-resident (VRAM allocation may lag the generate response).
    last_vram = -1
    for _ in range(30):
        try:
            r = httpx.get(_OLLAMA_PS_URL, timeout=5.0)
            models = r.json().get("models", [])
            for m in models:
                name = m.get("name", "") or m.get("model", "")
                if "qwen3-enrich" in name.lower():
                    last_vram = m.get("size_vram", 0)
                    if last_vram > 0:
                        return  # GPU-resident — pass
        except Exception:
            pass
        time.sleep(2)

    if last_vram == -1:
        pytest.fail(
            "qwen3-enrich:1.7b never appeared in /api/ps after 60s — "
            "model may not have loaded (check Ollama logs)"
        )
    else:
        pytest.fail(
            f"qwen3-enrich:1.7b has size_vram={last_vram} after 60s — "
            "running on CPU, not GPU (CPU fallback is forbidden)"
        )


def test_no_8b_model_resident():
    """U6 guard: daemon and test fixtures must NOT reload qwen3-query:8b after it's unloaded.

    qwen3-query:8b was retired in U3 (single resident model is now qwen3-enrich:1.7b).
    This test explicitly unloads the 8b model (keep_alive=0), then waits a few seconds,
    and verifies the daemon (sweeps paused by _quiesce_sweeps fixture) does NOT reload it.
    This proves no live code path automatically re-pins the retired 8b model.
    """
    # Step 1: explicitly unload qwen3-query:8b (keep_alive=0 instructs Ollama to unload immediately)
    import contextlib
    with contextlib.suppress(Exception):
        httpx.post(
            _OLLAMA_GEN_URL,
            json={"model": "qwen3-query:8b", "prompt": "", "keep_alive": 0},
            timeout=10.0,
        )

    # Step 2: wait a few seconds to give any background reload a chance to fire
    time.sleep(5)

    # Step 3: verify qwen3-query:8b is NOT resident (VRAM == 0 or absent from /api/ps)
    try:
        r = httpx.get(_OLLAMA_PS_URL, timeout=5.0)
        models = r.json().get("models", [])
    except Exception as exc:
        pytest.fail(f"Ollama /api/ps unreachable: {exc}")

    for m in models:
        name = m.get("name", "") or m.get("model", "")
        if "qwen3-query" in name.lower() and m.get("size_vram", 0) > 0:
            pytest.fail(
                f"qwen3-query:8b reloaded itself (size_vram={m.get('size_vram')}) after being "
                "explicitly unloaded — something in the daemon or a fixture is re-pinning the "
                "retired 8b model. Only qwen3-enrich:1.7b should be loaded (U3 single-model)."
            )


def test_ollama_enrich_model_throughput_floor():
    """A short prompt on qwen3-enrich:1.7b must complete under a generous ceiling.

    U3: throughput test now uses the single resident model (qwen3-enrich:1.7b).
    GPU is ~10× faster than CPU — the ceiling is generous enough to pass even
    under concurrent GPU load (thermal throttle, parallel enrich batches).
    """
    try:
        start = time.monotonic()
        r = httpx.post(
            _OLLAMA_GEN_URL,
            json={"model": "qwen3-enrich:1.7b", "prompt": "Say only: ok", "stream": False,
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
        assert elapsed < 30.0, f"Enrich LLM took {elapsed:.1f}s — unexpectedly slow (CPU fallback?)"


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
