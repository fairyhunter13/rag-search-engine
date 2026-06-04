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
    """HTTP client connected to the live daemon. Skips if daemon is not running."""
    with httpx.Client(base_url=DAEMON_URL, timeout=300.0) as client:
        try:
            client.get("/api/projects").raise_for_status()
        except Exception as exc:
            pytest.skip(f"Live daemon not available at {DAEMON_URL}: {exc}")
        yield client


@pytest.fixture(scope="session")
def gpu():
    """Verify CUDA GPU embedding is working. Skips if GPU is unavailable."""
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
    )
    if result.returncode != 0:
        pytest.skip(f"GPU embedding unavailable: {result.stderr[-300:]}")


@pytest.fixture(scope="session")
def project(http):
    """Return the path of an indexed project that has communities.

    Prefers larger projects (communities > 100) to ensure richer patterns data.
    Falls back to any project with communities if no large ones are found.
    """
    r = http.get("/api/projects")
    projects = r.json().get("projects", [])
    all_indexed = [p for p in projects if p.get("communities", 0) > 0]
    if not all_indexed:
        pytest.skip("No indexed project with communities — run build(action='pipeline') first")
    # Prefer a project with > 100 communities for richer test coverage
    large = [p["path"] for p in all_indexed if p.get("communities", 0) > 100]
    return large[0] if large else all_indexed[0]["path"]


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
