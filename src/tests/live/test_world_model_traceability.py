"""Live guard: Concept→Spec→Impl→Test traceability V&V (HR30).

Hard gates C1-C3: no orphan HR, no dangling principle, no phantom test, no untested HR,
no dead code anchor. Report-only C4: feature coverage with provenance. Writes stamp on pass.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def wm_report() -> dict:
    from opencode_search.kb.world_model import world_model_report
    return world_model_report(_ROOT)


def _gaps(report: dict, key: str) -> list[dict]:
    return report["gaps"][key]


class TestWorldModelTraceability:
    def test_no_doctrine_orphan_hr(self, wm_report: dict) -> None:
        orphans = [g for g in _gaps(wm_report, "c1_concept_spec") if g["kind"] == "hr_doctrine_orphan"]
        assert not orphans, f"HRs not cited by any §1a principle: {[g['ref'] for g in orphans]}"

    def test_no_dangling_principle_hr(self, wm_report: dict) -> None:
        dangling = [g for g in _gaps(wm_report, "c1_concept_spec") if g["kind"] == "principle_dangling_hr"]
        assert not dangling, f"Principles cite non-existent HRs: {[g['ref'] for g in dangling]}"

    def test_no_untested_hr(self, wm_report: dict) -> None:
        untested = [g for g in _gaps(wm_report, "c2_spec_test") if g["kind"] == "hr_untested"]
        assert not untested, f"HRs with no §14 row: {[g['ref'] for g in untested]}"

    def test_no_phantom_test_ref(self, wm_report: dict) -> None:
        phantom = [g for g in _gaps(wm_report, "c2_spec_test") if g["kind"] == "phantom_test_ref"]
        assert not phantom, f"§14 test names not collectable: {[g['ref'] for g in phantom]}"

    def test_no_dead_code_anchor(self, wm_report: dict) -> None:
        dead = _gaps(wm_report, "c3_spec_impl")
        assert not dead, f"Dead code anchors in §13b: {[g['ref'] for g in dead]}"

    def test_structural_validate_is_valid(self, wm_report: dict) -> None:
        sv = wm_report.get("structural_validate", {})
        assert "error" not in sv, f"structural validate error: {sv.get('error')}"
        assert sv.get("verdict") == "VALID", f"structural verdict={sv.get('verdict')!r}"

    def test_feature_coverage_reported(self, wm_report: dict) -> None:
        cov = wm_report.get("coverage", {})
        assert "feature_pct" in cov, "feature_pct missing from coverage"
        for g in _gaps(wm_report, "c4_feature_spec"):
            assert "detail" in g and "ref" in g, f"C4 gap missing provenance: {g}"

    def test_write_validation_stamp(self, wm_report: dict) -> None:
        assert wm_report["verdict"] == "VALID", (
            f"verdict={wm_report['verdict']!r} — hard gaps present, stamp not written"
        )
        from opencode_search.kb.world_model import write_stamp
        write_stamp("green")
