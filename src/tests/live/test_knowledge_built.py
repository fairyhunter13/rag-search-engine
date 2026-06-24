"""T1: knowledge-built coverage gate — every sufficiently large project must have communities.

Research grounding (June 2026):
- Community structure without ground truth validated by modularity, coverage, singleton ratio
  (arXiv 2501.07025; Leiden Q~0.53 = moderately well-formed).
- Composition invariants in federated systems are invisible to single-unit analysis
  (arXiv 2606.02019): a single non-enriched member degrades aggregate quality silently.
- The gap caught: projects could pass validity (verdict=VALID, kb_state=ready, enriched_pct=100)
  with 0 communities — those checks are vacuously true when detect_communities was skipped.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.live

_BASE = "http://127.0.0.1:8765"
_HDR = {"Content-Type": "application/json"}

# Minimum symbols for a member to be expected to have communities.
# Below this, 0 communities is legitimate (e.g. docs-only repos, thin roots).
_SYM_THRESHOLD = 50


def _reg(substr: str) -> str:
    from opencode_search.core.registry import list_projects
    return next((e.path for e in list_projects() if substr in e.path and e.enabled), "")


_PROJECTS = {
    "ose": str(Path(__file__).resolve().parents[3]),
    "astro": _reg("astro-project"),
    "payment": _reg("payment-gateway"),
}


def _status(path: str) -> dict:
    r = requests.post(
        f"{_BASE}/api/overview",
        json={"what": "status", "project_path": path},
        headers=_HDR,
        timeout=60,
    )
    assert r.status_code == 200, f"overview(status): HTTP {r.status_code}"
    return json.loads(r.text)


def _status_converged(path: str, timeout: int = 360) -> dict:
    """Read-only bounded poll — waits for kb_state=ready without triggering indexing."""
    import time
    s = _status(path)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if s.get("kb_state") == "ready":
            return s
        time.sleep(5)
        s = _status(path)
    return s  # last snapshot — assertions fire on genuine never-ready (no skip/mask)


@pytest.fixture(scope="module")
def status_by_key() -> dict[str, dict]:
    """Poll all projects concurrently so total wait = max(wait_per_project), not sum."""
    import concurrent.futures
    pairs = [(k, v) for k, v in _PROJECTS.items() if v]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(pairs)) as ex:
        futures = {ex.submit(_status_converged, v): k for k, v in pairs}
        return {futures[f]: f.result() for f in concurrent.futures.as_completed(futures)}


class TestKnowledgeBuiltCorrectly:
    """T1: every sufficiently large member must have ≥1 L1 community."""

    @pytest.mark.parametrize("key", ["ose", "astro", "payment"])
    def test_named_root_communities_positive(self, key: str, status_by_key: dict) -> None:
        """T1a: Named roots with ≥50 symbols must have ≥1 community."""
        s = status_by_key.get(key, {})
        if s.get("symbols", 0) < _SYM_THRESHOLD:
            return  # below threshold — legitimately no communities required
        assert s.get("communities", 0) > 0, (
            f"{key}: {s['symbols']} symbols but communities=0 — "
            "detect_communities was skipped (JSON-race victim)"
        )

    def test_astro_members_community_coverage(self, status_by_key: dict) -> None:
        """T1b: Every astro member with ≥50 symbols must have ≥1 community.

        Catches the composition-level gap where a single non-enriched member
        silently degrades aggregate overview/ask quality (arXiv 2606.02019).
        """
        members = status_by_key.get("astro", {}).get("members", [])
        violations = [
            f"{m['path']} (sym={m['symbols']}, comm={m['communities']})"
            for m in members
            if m.get("symbols", 0) >= _SYM_THRESHOLD and m.get("communities", 0) == 0
        ]
        assert not violations, (
            f"Federation members with ≥{_SYM_THRESHOLD} symbols but 0 communities:\n"
            + "\n".join(violations)
        )

    @pytest.mark.parametrize("key", ["ose", "astro", "payment"])
    def test_l1_enriched_pct_complete(self, key: str, status_by_key: dict) -> None:
        """T1c: L1 enrichment must be 100% for ready projects with communities."""
        s = status_by_key.get(key, {})
        if s.get("kb_state") != "ready":
            return  # kb_state checked independently by test_kb_state_ready
        if s.get("communities", 0) == 0:
            return  # legitimately empty — no enrichment expected
        pct = s.get("l1_enriched_pct", 0.0)
        assert pct == 100.0, (
            f"{key}: l1_enriched_pct={pct}% — some L1 communities lack DeepSeek summaries"
        )

    @pytest.mark.parametrize("key", ["ose", "astro", "payment"])
    def test_kb_state_ready(self, key: str, status_by_key: dict) -> None:
        """T1d: All named roots must be kb_state=ready."""
        s = status_by_key.get(key, {})
        assert s.get("kb_state") == "ready", (
            f"{key}: kb_state={s.get('kb_state')!r} — enrichment incomplete"
        )
