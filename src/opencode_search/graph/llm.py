"""KB-build + chat-fallback LLM client: cloud DeepSeek only. No local generative LLM."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path

_DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
_DEEPSEEK_MODEL = os.environ.get("OSE_DEEPSEEK_MODEL", "deepseek-chat")


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


