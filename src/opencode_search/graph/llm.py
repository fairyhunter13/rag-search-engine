"""KB-build + chat-fallback LLM client: cloud DeepSeek only. No local generative LLM."""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path

# ---------------------------------------------------------------------------
# Process-level LLM token accounting (deepseek_extract calls only).
# Accumulate into {category}.{metric} counters; read via llm_token_stats().
# ---------------------------------------------------------------------------

_token_lock = threading.Lock()
_llm_token_stats: dict[str, int] = {}


def _accumulate_llm_tokens(usage: dict, category: str) -> None:
    """Thread-safe accumulation of deepseek_extract usage into process-level stats."""
    with _token_lock:
        for k in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "completion_tokens"):
            key = f"{category}.{k}"
            _llm_token_stats[key] = _llm_token_stats.get(key, 0) + int(usage.get(k, 0))
        _llm_token_stats[f"{category}.calls"] = _llm_token_stats.get(f"{category}.calls", 0) + 1


def llm_token_stats() -> dict[str, int]:
    """Snapshot of accumulated LLM token usage across all tracked deepseek_extract calls."""
    with _token_lock:
        return dict(_llm_token_stats)

_DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
# Pin to deepseek-v4-flash: the `deepseek-chat` alias deprecates 2026-07-24 15:59 UTC.
# 1M context, 384K output, MoE 284B/13B-active, automatic prefix caching.
# Override with OSE_DEEPSEEK_MODEL env var for testing.
_DEEPSEEK_MODEL = os.environ.get("OSE_DEEPSEEK_MODEL", "deepseek-v4-flash")


@lru_cache(maxsize=1)
def deepseek_key() -> str | None:
    """DeepSeek API key from env, else parsed from ~/.bash_env (systemd daemon lacks it).

    Returns None when unavailable — callers must raise, not silently swallow.
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


def _ds_post(payload_dict: dict, timeout: int) -> dict:
    """POST to DeepSeek and return the parsed JSON response body."""
    key = deepseek_key()
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY not found (env or ~/.bash_env)")
    data = json.dumps(payload_dict).encode()
    req = urllib.request.Request(
        _DEEPSEEK_URL, data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as exc:
        raise RuntimeError(f"DeepSeek unreachable: {exc}") from exc


def deepseek_extract(
    stable_prefix: str, dynamic_tail: str, *,
    model: str = _DEEPSEEK_MODEL, timeout: int = 120, max_tokens: int = 4096,
) -> tuple[str, dict]:
    """JSON extraction with stable-prefix caching (non-thinking, json_object mode).

    Split prompt into (stable_prefix=system, dynamic_tail=user) so the system
    prompt is byte-identical across calls → high automatic-cache hit rate.
    Do NOT use strict JSON schemas (ExtractBench: strict forcing 86.9%→70.0%).
    Returns (content_str, usage_dict{prompt_cache_hit_tokens, ...}).
    """
    resp = _ds_post({
        "model": model,
        "messages": [
            {"role": "system", "content": stable_prefix},
            {"role": "user", "content": dynamic_tail},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0, "max_tokens": max_tokens, "stream": False,
    }, timeout)
    content = resp["choices"][0]["message"]["content"]
    u = resp.get("usage", {})
    return content, {
        "prompt_cache_hit_tokens": u.get("prompt_cache_hit_tokens", 0),
        "prompt_cache_miss_tokens": u.get("prompt_cache_miss_tokens", 0),
        "completion_tokens": u.get("completion_tokens", 0),
    }


