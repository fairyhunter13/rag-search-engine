"""T2: sample federation composed-entity validity — tests against the sample federation root.

Synthetic-fixture tests (test_federation_architecture/logical_entity) prove invariants
in isolation. This file validates that the sample shop-federation root holds the same
invariants — a composition gap invisible to single-unit analysis (arXiv 2606.02019).

Metamorphic testing principle (MetaRAG arXiv 2509.09360 / MeTMaP ACM 2024):
behavior must stay invariant under semantically-neutral transforms. Here:
  root-scoped search ⊇ member-scoped search  (fan-out monotonicity)
"""
from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = pytest.mark.live

_SYM_THRESHOLD = 50  # same as test_knowledge_built.py


@pytest.fixture(scope="module")
def fed_root(sample_workspace) -> str:
    from tests.live._sample_workspace import SampleWorkspace
    assert isinstance(sample_workspace, SampleWorkspace)
    return sample_workspace.fed_root


@pytest.fixture(scope="module")
def fed_status(fed_root) -> dict:
    import requests
    r = requests.post(
        "http://127.0.0.1:8765/api/overview",
        json={"what": "status", "project_path": fed_root},
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    assert r.status_code == 200
    return json.loads(r.text)


class TestFederationComposedEntity:
    """T2: sample federation root as one composed entity."""

    def test_member_list_non_empty(self, fed_status: dict) -> None:
        """T2a: federation must report ≥2 members (root + at least one member)."""
        members = fed_status.get("members", [])
        assert len(members) >= 2, f"Expected ≥2 members, got {len(members)}"

    def test_no_member_has_symbols_without_communities(self, fed_status: dict) -> None:
        """T2b: composition invariant — no member with ≥50 symbols may have 0 communities.

        A single non-enriched member silently degrades aggregate overview/ask quality.
        """
        violations = [
            f"{m['path']} (sym={m['symbols']}, comm={m['communities']})"
            for m in fed_status.get("members", [])
            if m.get("symbols", 0) >= _SYM_THRESHOLD and m.get("communities", 0) == 0
        ]
        assert not violations, (
            "Members with ≥50 symbols and 0 communities:\n" + "\n".join(violations)
        )

    def test_aggregate_symbols_matches_member_sum(self, fed_status: dict) -> None:
        """T2c: overview(status) aggregate symbols == Σ member symbols."""
        members = fed_status.get("members", [])
        member_sum = sum(m.get("symbols", 0) for m in members)
        root_total = fed_status.get("symbols", -1)
        assert root_total == member_sum, (
            f"Aggregate symbols={root_total} ≠ Σ member={member_sum}"
        )

    def test_aggregate_communities_matches_member_sum(self, fed_status: dict) -> None:
        """T2d: overview(status) aggregate communities == Σ member communities."""
        members = fed_status.get("members", [])
        member_sum = sum(m.get("communities", 0) for m in members)
        root_total = fed_status.get("communities", -1)
        assert root_total == member_sum, (
            f"Aggregate communities={root_total} ≠ Σ member={member_sum}"
        )

    @pytest.mark.slow
    def test_root_scoped_search_reaches_member_content(self, fed_root: str) -> None:
        """T2e: metamorphic fan-out — search([root]) reaches member content.

        Monotonicity: root-scoped search projects_searched must include member paths.
        """
        from opencode_search.daemon.federation import expand_federation
        from opencode_search.server.mcp import search as mcp_search

        members = expand_federation(fed_root)
        assert len(members) >= 2, "Need ≥2 members for fan-out test"
        data = json.loads(asyncio.run(mcp_search("function", project_paths=[fed_root])))
        searched = data.get("projects_searched", [])
        member_paths = [m for m in members if m != fed_root]
        covered = [m for m in member_paths if m in searched]
        assert covered, (
            f"search([root]) did not reach any members in projects_searched.\n"
            f"members: {member_paths[:3]}\nsearched: {searched[:5]}"
        )

    @pytest.mark.slow
    def test_all_members_kb_state_ready(self, fed_status: dict) -> None:
        """T2f: every federation member must be kb_state=ready."""
        not_ready = [
            f"{m['path']}: {m['kb_state']}"
            for m in fed_status.get("members", [])
            if m.get("kb_state") != "ready"
        ]
        assert not not_ready, "Members not ready:\n" + "\n".join(not_ready)
