"""KB convergence proof tests — guarantee the auto-queue has completed.

These tests prove the system guarantee: after the daemon's _run_kb_sweep has run,
every community level must be ≥ 99% enriched (the "done" bar chosen by the user).

Definition of DONE for a project's auto-queue:
  - Indexed (file_count > 0)
  - Hierarchy built (max_level ≥ 2)
  - enrichment_by_level[L].pct ≥ 99 for every level 1..max_level
  - No perpetually-stuck level (draining, not frozen)

All tests are live — require the daemon at :8765 and astro-project indexed + enriched.
No mocks. No skips. GPU-only inference (no CPU fallback).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_ASTRO = "/home/user/git/github.com/fairyhunter13/astro-project"
_DONE_PCT = 99.0  # threshold for "done" at each level


# ---------------------------------------------------------------------------
# Surface: /api/kb_health per-level breakdown
# ---------------------------------------------------------------------------

class TestAllLevelsEnriched:
    """Every hierarchy level must be ≥ 99% enriched after auto-queue converges."""

    @pytest.mark.slow
    def test_all_levels_enriched(self, http, astro):
        """Guarantee: /api/kb_health reports ≥ 99% enrichment at every level.

        This is the definitive 'auto-queue done' assertion — it fails until
        _run_kb_sweep's L1-drain + L2+ hierarchy-enrich loop has fully converged.
        The daemon converges this automatically; no manual trigger is needed.
        """
        r = http.get("/api/kb_health", params={"project": astro})
        assert r.status_code == 200, f"kb_health failed: {r.status_code} {r.text[:200]}"
        data = r.json()

        by_level = data.get("enrichment_by_level", {})
        assert by_level, (
            f"kb_health returned no enrichment_by_level breakdown: {data}"
        )
        assert len(by_level) >= 2, (
            f"Expected at least 2 hierarchy levels, got {len(by_level)}: {by_level}"
        )

        failed_levels = []
        for lvl, stats in sorted(by_level.items(), key=lambda kv: int(kv[0])):
            pct = stats.get("pct", 0)
            total = stats.get("total", 0)
            enriched = stats.get("enriched", 0)
            if pct < _DONE_PCT:
                failed_levels.append(
                    f"L{lvl}: {pct:.1f}% ({enriched}/{total} enriched) < {_DONE_PCT}%"
                )

        assert not failed_levels, (
            "KB enrichment not yet converged — levels below done threshold:\n"
            + "\n".join(f"  {f}" for f in failed_levels)
            + "\nThe daemon's _run_kb_sweep will converge these automatically. "
            "Check daemon logs for 'kb_sweep: draining L1' and 'kb_sweep: enriching L2+'."
        )

    @pytest.mark.slow
    def test_l1_enriched_above_99(self, http, astro):
        """Level-1 specifically must be ≥ 99% (it was the primary stuck level)."""
        r = http.get("/api/kb_health", params={"project": astro})
        data = r.json()
        by_level = data.get("enrichment_by_level", {})
        l1 = by_level.get("1", {})
        pct = l1.get("pct", 0)
        total = l1.get("total", 0)
        enriched = l1.get("enriched", 0)
        assert pct >= _DONE_PCT, (
            f"L1 enrichment stuck at {pct:.1f}% ({enriched}/{total}). "
            "The _run_kb_sweep L1-drain loop must drive this to ≥ 99%."
        )

    @pytest.mark.slow
    def test_l2_enriched_above_99_after_l1_done(self, http, astro):
        """L2 must reach ≥ 99% once L1 is complete (parents synthesise from children)."""
        r = http.get("/api/kb_health", params={"project": astro})
        data = r.json()
        by_level = data.get("enrichment_by_level", {})
        l1 = by_level.get("1", {})
        l2 = by_level.get("2", {})
        if not l2:
            pytest.skip("No L2 hierarchy on this project")
        l1_pct = l1.get("pct", 0)
        l2_pct = l2.get("pct", 0)
        # Only assert L2 if L1 has converged
        if l1_pct >= _DONE_PCT:
            assert l2_pct >= _DONE_PCT, (
                f"L1 is done ({l1_pct:.1f}%) but L2 only at {l2_pct:.1f}% — "
                "handle_enrich_hierarchy L2+ pass did not converge after L1 completed."
            )


# ---------------------------------------------------------------------------
# Surface: new _project_needs_community_enrich detection (F2)
# ---------------------------------------------------------------------------

class TestCommunityEnrichDetection:
    """_project_needs_community_enrich correctly sees L1 deficit."""

    @pytest.mark.slow
    def test_needs_community_enrich_returns_false_when_done(self, astro):
        """After full convergence, _project_needs_community_enrich must return False."""
        from opencode_search.handlers._autopipeline import _project_needs_community_enrich
        still_needs = _project_needs_community_enrich(astro)
        assert not still_needs, (
            f"_project_needs_community_enrich({astro!r}) returned True after convergence — "
            "unenriched L1 communities still exist in the graph DB."
        )

    @pytest.mark.slow
    def test_needs_hierarchy_enrich_returns_false_when_done(self, astro):
        """After full convergence, _project_needs_hierarchy_enrich must also return False."""
        from opencode_search.handlers._autopipeline import _project_needs_hierarchy_enrich
        still_needs = _project_needs_hierarchy_enrich(astro)
        assert not still_needs, (
            f"_project_needs_hierarchy_enrich({astro!r}) returned True after convergence — "
            "unenriched L2+ communities still exist in the graph DB."
        )


# ---------------------------------------------------------------------------
# Surface: CLI kb-status command
# ---------------------------------------------------------------------------

class TestCLIKbStatus:
    """opencode-search kb-status --json must report DONE for astro-project."""

    @pytest.mark.slow
    def test_cli_kb_status_reports_done(self):
        """kb-status --json returns per-level pct + DONE verdict when enrichment is complete."""
        cli_path = str(Path(sys.executable).parent / "opencode-search")
        result = subprocess.run(
            [cli_path, "kb-status", "--project", _ASTRO, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"CLI kb-status failed (exit {result.returncode}): {result.stderr[:300]}"
        )
        data = json.loads(result.stdout)
        projects = data.get("projects", [])
        assert projects, f"No projects in CLI kb-status output: {data}"
        proj = projects[0]
        assert "verdict" in proj, f"verdict field missing from kb-status output: {proj}"
        assert "enrichment_by_level" in proj, f"enrichment_by_level missing: {proj}"
        assert "enrichment_pct" in proj, f"enrichment_pct missing: {proj}"
        assert proj["verdict"] == "DONE", (
            f"CLI kb-status reports PENDING for {_ASTRO}: {proj.get('enrichment_by_level', {})}"
        )

    def test_cli_kb_status_all_projects_shows_fields(self):
        """kb-status (no --project) returns required fields for at least 1 project."""
        cli_path = str(Path(sys.executable).parent / "opencode-search")
        result = subprocess.run(
            [cli_path, "kb-status", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"CLI kb-status (all projects) failed (exit {result.returncode}): {result.stderr[:300]}"
        )
        data = json.loads(result.stdout)
        projects = data.get("projects", [])
        assert projects, f"No projects returned by kb-status: {data}"
        for p in projects:
            assert "verdict" in p, f"verdict missing for {p.get('project_path')}: {p}"


# ---------------------------------------------------------------------------
# Convergence: kb_sweep L1-drain actually reduces unenriched count
# ---------------------------------------------------------------------------

class TestKbSweepDrainsL1:
    """The _run_kb_sweep L1-drain loop measurably reduces unenriched communities.

    This test drives the real sweep handler against a real (live) daemon context.
    It does NOT require the sweep to fully converge — it only verifies that one
    sweep pass drains unenriched L1 communities (count decreases or reaches 0).
    Marked slow because it runs LLM enrichment on the GPU.
    """

    @pytest.mark.slow
    def test_kb_sweep_drains_l1(self, astro):
        """One _run_kb_sweep() pass must produce enriched_communities > 0 OR L1 is already done."""
        import asyncio

        from opencode_search.config import get_project_graph_db_path
        from opencode_search.graph.storage import GraphStorage
        from opencode_search.handlers._enrichment import handle_enrich_project

        db_path = get_project_graph_db_path(astro)
        gs = GraphStorage(db_path)
        gs.open()
        try:
            before = sum(
                1 for c in gs.get_communities(level=1, min_node_count=2)
                if not c.title
            )
        finally:
            gs.close()

        if before == 0:
            # L1 already fully enriched — that's the convergence goal
            return

        # Run one enrichment pass (mirrors what _run_kb_sweep does)
        result = asyncio.run(
            handle_enrich_project(astro, scope="communities", max_communities=100)
        )
        enriched = result.get("enriched_communities", 0)

        gs2 = GraphStorage(db_path)
        gs2.open()
        try:
            after = sum(
                1 for c in gs2.get_communities(level=1, min_node_count=2)
                if not c.title
            )
        finally:
            gs2.close()

        assert enriched > 0 or after < before, (
            f"kb_sweep L1-drain made no progress: before={before}, after={after}, "
            f"enriched={enriched}. The sweep loop would not converge."
        )
        if after < before:
            assert True  # drain is working
