"""Pick the `claude -p` subscription profile with the most headroom.

Ported out of vendor/docgen (ose_docgen.accounts) when tier 3 was deleted: dashboard
chat is the only remaining caller, and losing the selector would send every chat call
to the default profile again — the account-draining regression that fix was for.

No generative client lives here. This reads the OAuth *usage* endpoint only, to choose
which CLAUDE_CONFIG_DIR the chat subprocess should use.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
import urllib.request
from pathlib import Path

_CACHE_TTL_S = 300
_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

PROFILES: list[str] = [
    p.strip() for p in os.environ.get(
        "RSE_CLAUDE_PROFILES",
        f"{os.path.expanduser('~/.claude')},{os.path.expanduser('~/.claude-1')}",
    ).split(",") if p.strip()
]


def _token(config_dir: str) -> str | None:
    creds = Path(config_dir) / ".credentials.json"
    if not creds.exists():
        return None
    try:
        data = json.loads(creds.read_text(encoding="utf-8"))
    except Exception:
        return None
    flat = data.get("oauth_token") or data.get("access_token") or data.get("token")
    nested = data.get("claudeAiOauth") or {}
    return flat or nested.get("accessToken") or nested.get("access_token")


def utilization(config_dir: str) -> float | None:
    """max(5h, 7d) utilization as 0.0-1.0, or None if the profile is unusable.

    Cached for 5 minutes per profile; an unreadable endpoint reads as 0.0 (optimistic —
    a real 429 fails the call, which is a louder signal than a guess here).
    """
    if not _token(config_dir):
        return None
    cache = Path(config_dir) / "usage-exact.json"
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if time.time() - cached.get("_ts", 0) < _CACHE_TTL_S:
            return max(cached["five_hour_pct"], cached["seven_day_pct"])
    except Exception:
        pass
    req = urllib.request.Request(
        _USAGE_URL,
        headers={"Authorization": f"Bearer {_token(config_dir)}",
                 "anthropic-beta": "oauth-2025-04-20"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return 0.0
    # Nested (current) reports 0-100; the older flat shape reported 0.0-1.0.
    if "five_hour" in data:
        five, seven = ((data.get(k) or {}).get("utilization", 0) / 100.0
                       for k in ("five_hour", "seven_day"))
    else:
        five = float(data.get("five_hour_utilization", 0.0))
        seven = float(data.get("seven_day_utilization", 0.0))
    with contextlib.suppress(Exception):
        cache.write_text(json.dumps(
            {"five_hour_pct": five, "seven_day_pct": seven, "_ts": time.time()}), encoding="utf-8")
    return max(five, seven)


def pick_profile(profiles: list[str] | None = None) -> str | None:
    """Least-used profile that still has headroom, or None if all are exhausted."""
    scored = [(u, p) for p in (profiles if profiles is not None else PROFILES)
              if (u := utilization(p)) is not None and u < 1.0]
    return min(scored)[1] if scored else None


def subprocess_env(config_dir: str) -> dict[str, str]:
    """Env for the chat subprocess: subscription creds, never API billing.

    CLAUDE_CODE_SAFE_MODE avoids the parent-IPC deadlock when the daemon itself was
    started from inside an active Claude Code session.
    """
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["CLAUDE_CONFIG_DIR"] = config_dir
    env["CLAUDE_CODE_SAFE_MODE"] = "1"
    return env
