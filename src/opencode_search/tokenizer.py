"""Tokenizer utilities (local-only).

The embedder runs fully locally, so we only need a lightweight, deterministic
token estimate for chunk sizing.

This module intentionally avoids network access and heavy model downloads.
"""

from __future__ import annotations


def count_tokens_for_tier(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)
