"""Ollama HTTP client for GPU KB enrichment. FORBIDDEN for dashboard chat."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from opencode_search.core.config import LLM_MODEL
from opencode_search.core.gpu import assert_ollama_gpu

_OLLAMA_URL = os.environ.get("OPENCODE_OLLAMA_URL", "http://127.0.0.1:11434")


def chat(prompt: str, *, model: str = LLM_MODEL, timeout: int = 120) -> str:
    """Generate via Ollama on GPU. Only for KB build / enrichment, never dashboard chat."""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_gpu": -1},
    }).encode()
    req = urllib.request.Request(
        f"{_OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read()).get("response", "")
        assert_ollama_gpu(model, _OLLAMA_URL)
        return result
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama unreachable at {_OLLAMA_URL}: {exc}") from exc
