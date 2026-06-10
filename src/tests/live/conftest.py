"""Shared fixtures for live tests — all require real running services."""
from __future__ import annotations

import contextlib
import json
import re
import subprocess
import sys

import httpx
import pytest

DAEMON_URL = "http://localhost:8765"


@pytest.fixture(scope="session")
def http():
    """HTTP client connected to the live daemon."""
    # retries=2: handles stale keepalive connections (server closes after N requests)
    transport = httpx.HTTPTransport(retries=2)
    with httpx.Client(base_url=DAEMON_URL, timeout=300.0, transport=transport) as client:
        try:
            client.get("/api/projects").raise_for_status()
        except Exception as exc:
            pytest.fail(f"Live daemon not available at {DAEMON_URL}: {exc}")
        yield client


@pytest.fixture(scope="session")
def gpu():
    """Verify CUDA GPU embedding is working."""
    import os
    env = {**os.environ, "FASTEMBED_CACHE_PATH": os.path.expanduser("~/.cache/opencode/fastembed")}
    result = subprocess.run(
        [
            sys.executable, "-c",
            "from opencode_search.embeddings import embed_query; "
            "from opencode_search.config import DEFAULT_EMBED_MODEL, DEFAULT_DIMS; "
            "v = embed_query('test query', model=DEFAULT_EMBED_MODEL, dimensions=DEFAULT_DIMS); "
            "assert len(v) > 0, f'empty vector: {v}'",
        ],
        capture_output=True,
        text=True,
        cwd="/home/user/git/github.com/fairyhunter13/opencode-search-engine",
        env=env,
    )
    assert result.returncode == 0, f"GPU embedding unavailable: {result.stderr[-300:]}"


_ASTRO_PATH = "/home/user/git/github.com/fairyhunter13/astro-project"


@pytest.fixture(scope="session")
def astro(http):
    """Return astro-project path. Fails if not indexed with communities."""
    r = http.get("/api/projects")
    projects = r.json().get("projects", [])
    match = next((p for p in projects if p.get("path") == _ASTRO_PATH), None)
    assert match is not None, f"astro-project not in registry: {_ASTRO_PATH}"
    assert match.get("communities", 0) > 0, (
        "astro-project has no communities — run build(action='pipeline') first"
    )
    return _ASTRO_PATH


@pytest.fixture(scope="session")
def project(http):
    """Return the path of an indexed project that has communities.

    Prefers astro-project as the canonical test target; falls back to any
    large indexed project if astro-project is unavailable.
    """
    r = http.get("/api/projects")
    projects = r.json().get("projects", [])
    all_indexed = [p for p in projects if p.get("communities", 0) > 0]
    assert all_indexed, "No indexed project with communities — run build(action='pipeline') first"
    # Prefer astro-project as canonical test target
    astro_match = next((p for p in all_indexed if p.get("path") == _ASTRO_PATH), None)
    if astro_match:
        return _ASTRO_PATH
    # Fall back to largest well-enriched project
    large = sorted(
        [p for p in all_indexed if p.get("communities", 0) > 100],
        key=lambda p: p.get("communities", 0),
        reverse=True,
    )
    for candidate in large[:8]:
        try:
            h = http.get("/api/kb_health", params={"project": candidate["path"]})
            if h.status_code == 200 and h.json().get("enrichment_pct", 0) > 50:
                return candidate["path"]
        except Exception:
            continue
    if large:
        return large[0]["path"]
    return all_indexed[0]["path"]


@pytest.fixture(scope="session")
def quality_project(http):
    """Return opencode-search-engine path for engine-specific quality tests.

    Tests that reference engine-internal symbols (handle_chat_auto,
    handle_debug_trace) must use this fixture instead of the generic `project`.
    """
    r = http.get("/api/projects")
    projects = r.json().get("projects", [])
    matches = [p["path"] for p in projects if p.get("path", "").endswith("opencode-search-engine")]
    assert matches, "opencode-search-engine not in registry — run build(action='pipeline') first"
    return matches[0]


def parse_sse(response: httpx.Response) -> list[dict]:
    """Parse text/event-stream response into a list of event dicts."""
    events: list[dict] = []
    for line in response.text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                with contextlib.suppress(json.JSONDecodeError):
                    events.append(json.loads(payload))
    return events


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    """Minimal browser context: fixed viewport, ignore self-signed certs."""
    return {**browser_context_args, "viewport": {"width": 1280, "height": 720},
            "ignore_https_errors": True}


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    """Single Chromium launch per session; no sandbox/GPU/extensions → low CPU/RAM."""
    return {**browser_type_launch_args,
            "headless": True,
            "args": ["--no-sandbox", "--disable-dev-shm-usage",
                     "--disable-gpu", "--disable-extensions",
                     "--disable-background-networking",
                     "--disable-renderer-backgrounding",
                     "--mute-audio", "--no-first-run"]}


@pytest.fixture(scope="session", autouse=True)
def _pin_ollama_model_resident():
    """Keep qwen3-query:8b loaded for the whole test session (no per-test warm-up).
    Reverts to 10m keep-alive at teardown to match the production systemd setting."""
    with contextlib.suppress(Exception):
        httpx.post("http://localhost:11434/api/generate",
                   json={"model": "qwen3-query:8b", "prompt": "Hello", "stream": False,
                         "options": {"num_predict": 1}, "keep_alive": -1},
                   timeout=30.0)
    yield
    with contextlib.suppress(Exception):
        httpx.post("http://localhost:11434/api/generate",
                   json={"model": "qwen3-query:8b", "prompt": "", "keep_alive": "10m"},
                   timeout=5.0)


@pytest.fixture(scope="session", autouse=True)
def _cap_cpu_threads():
    """Cap CPU math thread pools to 1 — embeddings run on CUDA, not CPU."""
    import os
    for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS",
              "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS",
              "ONNXRUNTIME_NUM_THREADS"):
        os.environ.setdefault(k, "1")
    yield


def judge_answer(answer: str, question: str) -> int:
    """Score an LLM answer 1-5 using the local query LLM. Returns 1 on failure."""
    try:
        from opencode_search.enricher.client import create_query_llm_client
        client = create_query_llm_client()
        prompt = (
            f"Score the following answer 1-5 for: {question}\n"
            f"Answer: {answer[:2000]}\n"
            "Respond with a single digit 1-5. Nothing else."
        )
        raw = client.chat([{"role": "user", "content": prompt}], max_tokens=8)
        m = re.search(r"[1-5]", raw)
        return int(m.group()) if m else 1
    except Exception:
        return 1
