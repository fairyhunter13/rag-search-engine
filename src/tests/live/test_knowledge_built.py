"""T1: knowledge-built coverage gate — every sufficiently large project must have communities.

Research grounding (June 2026):
- Community structure without ground truth validated by modularity, coverage, singleton ratio
  (arXiv 2501.07025; Leiden Q~0.53 = moderately well-formed).
- Composition invariants in federated systems are invisible to single-unit analysis
  (arXiv 2606.02019): a single member with no communities degrades aggregate quality silently.
- The gap caught: a project could pass validity (verdict=VALID) with 0 communities — that
  check is vacuously true when detect_communities was skipped.

The two other signals this docstring used to cite alongside verdict — `kb_state=ready` and
`enriched_pct=100` — left with tier 3, and their removal does not weaken the gate: they were
named here as examples of checks that pass vacuously, i.e. as the problem rather than the
guard. What the tests below assert is unchanged, because communities are graph clustering
plus `label_community_structural`, neither of which ever involved an LLM.

On the algorithm named above: detection is igraph `community_fastgreedy` (agglomerative
Clauset-Newman-Moore modularity, ALGO_VERSION "fg1"), with edgeless symbols grouped by
directory. It is not Leiden — Leiden was replaced by an exact k-shell partition, which was
in turn replaced by fastgreedy because k-core is a node ranking rather than a partition and
fragmented connected nodes into singletons. The arXiv 2501.07025 grounding above is cited
for the no-ground-truth validation method (modularity, coverage, singleton ratio), which
still applies; its Leiden Q figure is a scale anchor from the paper, not this repo's number.
"""
from __future__ import annotations

import json

import pytest
import requests

from tests.live._sample_workspace import SampleWorkspace

pytestmark = pytest.mark.live

_BASE = "http://127.0.0.1:8765"
_HDR = {"Content-Type": "application/json"}

# Minimum symbols for a member to be expected to have communities.
# Below this, 0 communities is legitimate (e.g. docs-only repos, thin roots).
_SYM_THRESHOLD = 50

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
def status_by_key(sample_workspace: SampleWorkspace) -> dict[str, dict]:
    """Snapshot status for all projects (non-polling — durable structural gates only)."""
    projects = {
        "service": sample_workspace.promo,
        "federation": sample_workspace.fed_root,
        "standalone": sample_workspace.ledger,
    }
    return {k: _status(v) for k, v in projects.items() if v}


class TestKnowledgeBuiltCorrectly:
    """T1: every sufficiently large member must have ≥1 L1 community."""

    @pytest.mark.parametrize("key", ["service", "federation", "standalone"])
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

        Catches the composition-level gap where a single uncommunitied member
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

