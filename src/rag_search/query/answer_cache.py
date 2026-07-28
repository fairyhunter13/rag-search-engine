"""Persistent file-based answer cache with TTL for pre-computed ask responses."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


def _key_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")


def get(cache_dir: Path, key: str) -> str | None:
    p = _key_path(cache_dir, key)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        if time.time() > data["expires"]:
            p.unlink(missing_ok=True)
            return None
        return data["value"]
    except Exception:
        return None


def set(cache_dir: Path, key: str, value: str, ttl_s: int = 86_400) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    _key_path(cache_dir, key).write_text(
        json.dumps({"value": value, "expires": time.time() + ttl_s})
    )


def invalidate(cache_dir: Path) -> None:
    """Remove all cached entries from cache_dir."""
    if cache_dir.exists():
        for f in cache_dir.glob("*.json"):
            f.unlink(missing_ok=True)
