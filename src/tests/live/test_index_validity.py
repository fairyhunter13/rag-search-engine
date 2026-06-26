"""V: index validity e2e — overview(what="validate") must return VALID for the 3 canonical projects.
Checks: no orphan chunks/vectors, no dangling edges, no bad community refs,
no placeholder L1 titles, no path leakage, process edges anchored + in-band.
"""
from __future__ import annotations

import json

import pytest
import requests

from tests.live._sample_workspace import SampleWorkspace

pytestmark = pytest.mark.live

_BASE = "http://127.0.0.1:8765"
_HDR = {"Content-Type": "application/json"}
@pytest.fixture(scope="session")
def index_projects(sample_workspace: SampleWorkspace) -> dict[str, str]:
    return {
        "service": sample_workspace.promo,
        "federation": sample_workspace.fed_root,
        "standalone": sample_workspace.ledger,
    }


@pytest.fixture(scope="session")
def validate_reports(index_projects: dict[str, str]) -> dict:
    from opencode_search.core.registry import list_projects
    all_proj = list(list_projects())
    for key, path in index_projects.items():
        if not path:
            pytest.fail(f"Project '{key}' not found in registry — register + index it first")
        ep = next((p for p in all_proj if p.path == path and p.enabled), None)
        if ep is None:
            pytest.fail(f"Project '{key}' not enabled in registry")
        is_fed_root = bool(getattr(ep, "federation", None))
        if not ep.indexed_at and not is_fed_root:
            pytest.fail(f"Project '{key}' has no indexed_at — index it first")
    reports: dict[str, dict] = {}
    for key, path in index_projects.items():
        r = requests.post(
            f"{_BASE}/api/overview",
            json={"what": "validate", "project_path": path},
            headers=_HDR,
            timeout=120,
        )
        assert r.status_code == 200, f"overview(validate,{key}): HTTP {r.status_code} — {r.text[:200]}"
        reports[key] = json.loads(r.text)
    return reports


def _chk(reports: dict, key: str) -> dict:
    return reports[key].get("checks", {})


def _root_member_chk(reports: dict, projects: dict[str, str], key: str) -> dict | None:
    path = projects[key]
    return next(
        (m["checks"] for m in reports[key].get("members", []) if m["path"] == path),
        None,
    )


class TestIndexValidity:
    @pytest.mark.parametrize("key", ["service", "federation", "standalone"])
    def test_verdict_valid(self, key: str, validate_reports: dict) -> None:
        r = validate_reports[key]
        failing = {k: v for k, v in r.get("checks", {}).items()
                   if (isinstance(v, int) and v != 0) or v is False}
        assert r.get("verdict") == "VALID", f"{key}: verdict={r.get('verdict')!r} failing={failing}"

    @pytest.mark.parametrize("key", ["service", "federation", "standalone"])
    def test_member_count_positive(self, key: str, validate_reports: dict) -> None:
        assert validate_reports[key]["member_count"] > 0

    @pytest.mark.parametrize("key", ["service", "federation", "standalone"])
    def test_chunk_count_positive(self, key: str, validate_reports: dict) -> None:
        assert _chk(validate_reports, key).get("chunk_count", 0) > 0

    @pytest.mark.parametrize("key", ["service", "federation", "standalone"])
    def test_no_orphan_chunks(self, key: str, validate_reports: dict) -> None:
        assert _chk(validate_reports, key).get("orphan_count", 0) == 0

    @pytest.mark.parametrize("key", ["service", "federation", "standalone"])
    def test_embedding_dim_768(self, key: str, validate_reports: dict) -> None:
        assert _chk(validate_reports, key).get("embedding_dim") == 768

    @pytest.mark.parametrize("key", ["service", "federation", "standalone"])
    def test_no_dangling_edges(self, key: str, validate_reports: dict) -> None:
        assert _chk(validate_reports, key).get("dangling_edges", 0) == 0

    @pytest.mark.parametrize("key", ["service", "federation", "standalone"])
    def test_no_bad_community_refs(self, key: str, validate_reports: dict) -> None:
        assert _chk(validate_reports, key).get("bad_community_refs", 0) == 0

    @pytest.mark.parametrize("key", ["service", "federation", "standalone"])
    def test_no_placeholder_communities(self, key: str, validate_reports: dict) -> None:
        assert _chk(validate_reports, key).get("placeholder_communities", 0) == 0

    @pytest.mark.parametrize("key", ["service", "federation", "standalone"])
    def test_no_path_leakage(self, key: str, validate_reports: dict) -> None:
        assert _chk(validate_reports, key).get("path_leakage", 0) == 0

    @pytest.mark.parametrize("key", ["service", "federation", "standalone"])
    def test_indexed_at_fresh(self, key: str, validate_reports: dict) -> None:
        assert _chk(validate_reports, key).get("indexed_at_fresh") is True

    def test_federation_root_has_process_graph(self, validate_reports: dict) -> None:
        chk = _chk(validate_reports, "federation")
        pg = chk.get("process_graph")
        assert pg is not None, "federation root should carry process_graph summary"
        assert "error" not in pg, f"process_graph error: {pg.get('error')}"

    def test_federation_process_edges_anchored(self, validate_reports: dict) -> None:
        pg = _chk(validate_reports, "federation").get("process_graph", {})
        assert pg.get("unanchored", 0) == 0, f"unanchored process edges: {pg}"

    def test_federation_process_edges_confidence_in_band(self, validate_reports: dict) -> None:
        pg = _chk(validate_reports, "federation").get("process_graph", {})
        assert pg.get("out_of_band", 0) == 0, f"out-of-band confidence: {pg}"

    @pytest.mark.parametrize("key", ["service", "standalone"])
    def test_standalone_no_process_graph(
        self, key: str, validate_reports: dict, index_projects: dict[str, str]
    ) -> None:
        root_chk = _root_member_chk(validate_reports, index_projects, key)
        assert root_chk is not None, f"root member not found for {key}"
        assert root_chk.get("process_graph") is None, (
            f"{key} is standalone but has process_graph: {root_chk.get('process_graph')}"
        )
