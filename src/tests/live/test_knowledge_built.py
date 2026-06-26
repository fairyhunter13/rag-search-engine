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


from tests.live._projects import federation_root as _federation_root
from tests.live._projects import standalone_project as _standalone_project

_PROJECTS = {
    "ose": str(Path(__file__).resolve().parents[3]),
    "federation": _federation_root(),
    "standalone": _standalone_project(),
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


@pytest.fixture(scope="module")
def status_by_key() -> dict[str, dict]:
    """Snapshot status for all projects (non-polling — durable structural gates only)."""
    return {k: _status(v) for k, v in _PROJECTS.items() if v}


class TestKnowledgeBuiltCorrectly:
    """T1: every sufficiently large member must have ≥1 L1 community."""

    @pytest.mark.parametrize("key", ["ose", "federation", "standalone"])
    def test_named_root_communities_positive(self, key: str, status_by_key: dict) -> None:
        """T1a: Indexed roots with ≥50 symbols must have ≥1 community."""
        s = status_by_key.get(key, {})
        if s.get("symbols", 0) < _SYM_THRESHOLD:
            return  # below threshold — legitimately no communities required
        assert s.get("communities", 0) > 0, (
            f"{key}: {s['symbols']} symbols but communities=0 — "
            "detect_communities was skipped (JSON-race victim)"
        )

    def test_federation_members_community_coverage(self, status_by_key: dict) -> None:
        """T1b: Every federation member with ≥50 symbols must have ≥1 community.

        Catches the composition-level gap where a single non-enriched member
        silently degrades aggregate overview/ask quality (arXiv 2606.02019).
        """
        members = status_by_key.get("federation", {}).get("members", [])
        violations = [
            f"{m['path']} (sym={m['symbols']}, comm={m['communities']})"
            for m in members
            if m.get("symbols", 0) >= _SYM_THRESHOLD and m.get("communities", 0) == 0
        ]
        assert not violations, (
            f"Federation members with ≥{_SYM_THRESHOLD} symbols but 0 communities:\n"
            + "\n".join(violations)
        )

