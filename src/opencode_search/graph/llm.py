"""LLM clients for KB enrichment: local Ollama (GPU) + cloud DeepSeek. FORBIDDEN for dashboard chat."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path

from opencode_search.core.config import LLM_MODEL
from opencode_search.core.gpu import assert_ollama_gpu

_OLLAMA_URL = os.environ.get("OPENCODE_OLLAMA_URL", "http://127.0.0.1:11434")
_DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
_DEEPSEEK_MODEL = os.environ.get("OSE_DEEPSEEK_MODEL", "deepseek-chat")


@lru_cache(maxsize=1)
def deepseek_key() -> str | None:
    """DeepSeek API key from env, else parsed from ~/.bash_env (systemd daemon lacks it).

    Returns None when unavailable so callers can fall back to local Ollama / templated output.
    """
    k = os.environ.get("DEEPSEEK_API_KEY")
    if k:
        return k.strip()
    try:
        for line in (Path.home() / ".bash_env").read_text().splitlines():
            s = line.strip()
            if s.startswith(("export DEEPSEEK_API_KEY=", "DEEPSEEK_API_KEY=")):
                return s.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def deepseek_chat(
    prompt: str, *, model: str = _DEEPSEEK_MODEL, timeout: int = 120,
    temperature: float = 0.0, max_tokens: int = 2048,
) -> str:
    """Generate via DeepSeek (OpenAI-compatible). Cloud lane — no local GPU. Raises if no key."""
    key = deepseek_key()
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY not found (env or ~/.bash_env)")
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        _DEEPSEEK_URL, data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]
    except urllib.error.URLError as exc:
        raise RuntimeError(f"DeepSeek unreachable: {exc}") from exc


def chat(
    prompt: str, *, model: str = LLM_MODEL, timeout: int = 120,
    temperature: float | None = None, num_predict: int | None = None,
    think: bool | None = False,
) -> str:
    """Generate via Ollama on GPU. Only for KB build / enrichment, never dashboard chat.

    temperature=0.0 forces greedy/deterministic decoding (used by classification so
    semantic_type is reproducible across runs); None keeps Ollama's default.
    think=False (DEFAULT for all KB calls) disables qwen3 thinking-mode. This is THE fix
    for the idle-CPU root cause: qwen3 is a thinking model and the modelfile's text
    "no <think>" instruction is only a hint it ignores — it then emits unbounded <think>
    output that hits the 4096 context window → Ollama truncates → llama-server busy-spins
    a core at ~84% indefinitely (Ollama #13461). The API `"think": false` is a hard control:
    clean bounded output, no truncation, ~2.8% idle CPU. Enrichment never wants reasoning,
    so False is the safe default; pass think=None only to deliberately allow it.
    """
    options: dict = {"num_gpu": -1}
    if temperature is not None:
        options["temperature"] = temperature
    if num_predict is not None:
        options["num_predict"] = num_predict
    body: dict = {"model": model, "prompt": prompt, "stream": False, "options": options}
    if think is not None:
        body["think"] = think
    payload = json.dumps(body).encode()
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
