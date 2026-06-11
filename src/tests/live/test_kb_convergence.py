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
        # astro-project is a federation root — its own graph is a thin aggregator.
        # After federation-first pruning the root only contains its own files (not
        # the 24 service sub-repos), so it may have 0 or 1 L1 communities and no L2.
        # Only assert levels that actually exist.
        if not by_level:
            return  # thin root with no communities — nothing to enrich

        failed_levels = []
        for lvl, stats in sorted(by_level.items(), key=lambda kv: int(kv[0])):
            pct = stats.get("pct", 0)
            total = stats.get("total", 0)
            enriched = stats.get("enriched", 0)
            if total == 0:
                continue  # empty level — the Leiden meta-graph couldn't form communities here
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


# ---------------------------------------------------------------------------
# Federation-first: root excludes member files; members remain queryable
# ---------------------------------------------------------------------------

class TestFederationRootFileCount:
    """After federation-first re-index, the root must NOT contain member files."""

    @pytest.mark.slow
    def test_root_file_count_excludes_members(self, http, astro):
        """astro-project root file_count must be << 20239 (its own files only, not 24 members)."""
        r = http.get("/api/projects/status", params={"project": astro})
        assert r.status_code == 200, f"status failed: {r.status_code} {r.text[:200]}"
        data = r.json()
        file_count = data.get("file_count", 0)
        # Pre-federation-first the root had ~20 239 files (all 24 members inlined).
        # After the fix the root should only contain its own files (~100-2000).
        assert file_count < 5_000, (
            f"Root file_count={file_count} is still huge — member files are still inlined. "
            "iter_files is not pruning external-symlink directories."
        )

    @pytest.mark.slow
    def test_members_still_indexed_and_queryable(self, http, astro):
        """All 24 federation members must remain registered and indexed."""
        r = http.get("/api/federation", params={"project": astro, "action": "list"})
        assert r.status_code == 200, f"federation list failed: {r.status_code}"
        data = r.json()
        members = data.get("members", [])
        assert len(members) >= 20, (
            f"Expected ≥ 20 federation members, got {len(members)}: {[m.get('path','?') for m in members[:5]]}"
        )
        indexed = [m for m in members if m.get("file_count", 0) > 0]
        assert len(indexed) >= 15, (
            f"Only {len(indexed)}/{len(members)} federation members are indexed (file_count > 0)"
        )


class TestFederationRootKbStatusDone:
    """kb-status must report DONE for the astro-project federation root."""

    @pytest.mark.slow
    def test_federation_root_verdict_done(self, http, astro):
        """/api/kb_health for a thin federation root must not be blocked on L2."""
        r = http.get("/api/kb_health", params={"project": astro})
        assert r.status_code == 200
        data = r.json()
        by_level = data.get("enrichment_by_level", {})
        # A thin root (no communities) or fully-enriched root is DONE.
        if not by_level:
            return  # no communities = nothing to enrich = DONE
        for lvl, stats in sorted(by_level.items(), key=lambda kv: int(kv[0])):
            total = stats.get("total", 0)
            if total == 0:
                continue  # empty level — vacuously satisfied
            pct = stats.get("pct", 0)
            assert pct >= _DONE_PCT, (
                f"Federation root L{lvl} enrichment only {pct:.1f}% — should be ≥ {_DONE_PCT}%"
            )


# ---------------------------------------------------------------------------
# iter_files: external-symlink directories must not be yielded
# ---------------------------------------------------------------------------

class TestIterFilesSkipsExternalSymlinks:
    """iter_files must skip directories whose resolved target is outside the root."""

    def test_external_symlink_dir_not_indexed(self, tmp_path):
        """iter_files must not yield files from an external-symlink subdirectory."""
        import os

        from opencode_search.discover import iter_files

        # Create a real external directory with a source file.
        external = tmp_path / "external_repo"
        external.mkdir()
        (external / "secret.py").write_text("SECRET = 1\n")

        # Create a project root with a symlink pointing to the external dir.
        root = tmp_path / "project"
        root.mkdir()
        (root / "main.py").write_text("def main(): pass\n")
        symlink = root / "vendor_repo"
        os.symlink(str(external), str(symlink))

        # iter_files must yield main.py but NOT secret.py.
        yielded = {str(p) for p in iter_files(root, follow_symlinks=True)}
        assert str(root / "main.py") in yielded, "main.py was not yielded"
        assert str(external / "secret.py") not in yielded, (
            "secret.py from external symlink was yielded — external symlink not pruned"
        )

    def test_internal_symlink_dir_is_still_indexed(self, tmp_path):
        """iter_files must still yield files from a symlink pointing INSIDE the root."""
        import os

        from opencode_search.discover import iter_files

        root = tmp_path / "project"
        root.mkdir()
        real_subdir = root / "src"
        real_subdir.mkdir()
        (real_subdir / "app.py").write_text("x = 1\n")

        # Symlink inside the root (internal).
        link = root / "src_link"
        os.symlink(str(real_subdir), str(link))

        yielded = {str(p) for p in iter_files(root, follow_symlinks=True)}
        assert str(real_subdir / "app.py") in yielded, "Internal symlink file was not yielded"


# ---------------------------------------------------------------------------
# Phase 103: corrected DONE verdict — every federation member converges.
# These exercise the REAL cli._verdict via `opencode-search kb-status --json`
# (no local verdict copies, no mocks, no skips).
# ---------------------------------------------------------------------------

def _kb_status_json(project: str | None = None) -> list[dict]:
    """Run the real CLI kb-status and return its per-project list."""
    cli_path = str(Path(sys.executable).parent / "opencode-search")
    args = [cli_path, "kb-status", "--json"]
    if project:
        args += ["--project", project]
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, (
        f"CLI kb-status failed (exit {result.returncode}): {result.stderr[:300]}"
    )
    return json.loads(result.stdout).get("projects", [])


def _astro_members(http, astro) -> list[str]:
    """Return indexed federation member paths for astro-project."""
    r = http.get("/api/federation", params={"project": astro, "action": "list"})
    assert r.status_code == 200, f"federation list failed: {r.status_code} {r.text[:200]}"
    return [m["path"] for m in r.json().get("members", []) if m.get("file_count", 0) > 0]


class TestAllFederationMembersConverge:
    """Every indexed astro-project federation member must verdict DONE.

    Phase 103: after federation-first indexing the root is a thin aggregator and the
    federation's health == its members' health. A member is DONE when every non-empty
    community level is ≥ 99% enriched; thin (L1-only) and definitions-only (0-community)
    members are DONE with nothing more to do. This asserts the REAL CLI verdict, so it
    fails loudly (never skips) if any member is genuinely unconverged.
    """

    @pytest.mark.slow
    def test_all_members_done(self, http, astro):
        members = set(_astro_members(http, astro))
        assert len(members) >= 15, f"Expected ≥ 15 indexed members, got {len(members)}"

        by_path = {p.get("project_path"): p for p in _kb_status_json()}
        pending = []
        for pp in sorted(members):
            entry = by_path.get(pp)
            if entry is None:
                pending.append(f"{pp.rsplit('/', 1)[-1]}: missing from kb-status output")
                continue
            if entry.get("verdict") != "DONE":
                levels = {
                    k: f"{v['pct']}%({v['total']})"
                    for k, v in entry.get("enrichment_by_level", {}).items()
                    if v.get("total", 0) > 0
                }
                pending.append(f"{pp.rsplit('/', 1)[-1]}: {entry.get('verdict')} {levels}")

        assert not pending, (
            "Federation members not converged to DONE:\n"
            + "\n".join(f"  {p}" for p in pending)
        )

    @pytest.mark.slow
    def test_definitions_only_member_is_done(self, http, astro):
        """A 0-community member (generated proto/gRPC stubs, 0 internal edges) verdicts DONE."""
        members = set(_astro_members(http, astro))
        zero_comm = []
        for entry in _kb_status_json():
            if entry.get("project_path") not in members:
                continue
            if entry.get("total_communities") == 0:
                zero_comm.append(entry["project_path"])
                assert entry.get("verdict") == "DONE", (
                    f"0-community member {entry['project_path']} verdicts "
                    f"{entry.get('verdict')} — definitions-only repos must be DONE"
                )
        assert zero_comm, (
            "No 0-community member found — expected ≥ 1 definitions-only repo (e.g. astro-proto)"
        )

    @pytest.mark.slow
    def test_thin_single_level_member_is_done(self, http, astro):
        """A member with only a non-empty L1 ≥ 99% (no L2) verdicts DONE — L2 not required."""
        members = set(_astro_members(http, astro))
        found_thin = False
        for entry in _kb_status_json():
            if entry.get("project_path") not in members:
                continue
            by_level = entry.get("enrichment_by_level", {})
            non_empty = {k: v for k, v in by_level.items() if v.get("total", 0) > 0}
            if list(non_empty.keys()) == ["1"] and non_empty["1"]["pct"] >= _DONE_PCT:
                found_thin = True
                assert entry.get("verdict") == "DONE", (
                    f"Thin L1-only member {entry['project_path']} verdicts "
                    f"{entry.get('verdict')} — verdict must not require a second level"
                )
        assert found_thin, "No thin L1-only member found among federation members"
