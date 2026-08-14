"""HTTP surface matrix — all mounted routes not covered by test_server.

Routes excluded (already in test_p5): /healthz, /dashboard, /api/projects,
/api/overview, /api/auto_pipeline_status, /mcp. (/api/suggested_questions was on this list and
is now on test_p5's `deleted` one.)

Routes verified here — this is the *surviving* half after R0; the tier-3 rows this
docstring used to list are asserted absent by test_server.py::test_e7_trimmed_http_surface:
  GET /               → 200 or redirect
  GET /api/metrics    → 200, and no LLM token accounting
  POST /api/sweeps/*  → 200
  GET /api/graph_export  → 200, node/edge structure
  GET /api/storage_health → 200
"""
from __future__ import annotations

import pytest

from tests.live._sample_workspace import SampleWorkspace
from tests.live._sweeps import sweeps_state

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def project(sample_workspace: SampleWorkspace) -> str:
    return sample_workspace.promo


def test_root_not_500(live_client):
    r = live_client.get("/", allow_redirects=False)
    assert r.status_code < 500, f"GET / returned server error {r.status_code}"


def test_api_metrics_no_llm_accounting(live_client):
    # This asserted the *presence* of `llm_tokens` — the per-category DeepSeek counters
    # `graph/llm.py::_accumulate_llm_tokens` fed (enrich/classify/bpre/bpre_link). It went red
    # the moment that module left, so it is inverted rather than deleted: the route survives and
    # still reports the deterministic lanes, and the absence of `llm_tokens` is now the cheapest
    # runtime witness that no cloud LLM remains behind an HTTP surface (DK2, from the wire side).
    r = live_client.get("/api/metrics")
    assert r.status_code == 200
    data = r.json()
    assert "llm_tokens" not in data, f"LLM token accounting is back on /api/metrics: {list(data)}"
    assert {"search", "rerank", "cpu"} <= set(data), f"/api/metrics lost a lane: {list(data)}"


def test_api_sweeps_pause_resume(live_client):
    """Both routes work, and each reports the state it replaced.

    The inner `previously_paused is True` is also this suite's ordering gate: the session
    fixture pauses sweeps for the whole run, so if any earlier-sorting file resumed
    unconditionally instead of restoring what it found, the pause is gone by the time this
    runs and the assert says so. `test_cpu_budget` (c) sorts before `test_http_surface` (h),
    which is exactly the pair that regressed. `live_client` is taken (unused) so an
    unreachable daemon fails with conftest's actionable message, not a raw ConnectionError.
    """
    with sweeps_state(paused=True) as paused:
        assert paused["status"] == "paused"
        with sweeps_state(paused=False) as resumed:
            assert resumed["status"] == "resumed"
            assert resumed["previously_paused"] is True, (
                "sweeps were already running when this test began — the session-scoped "
                "pause_sweeps fixture was cancelled by an earlier test resuming "
                "unconditionally instead of restoring the state it found"
            )



def test_api_graph_export(live_client, project):
    r = live_client.get(f"/api/graph_export?project={project}")
    assert r.status_code == 200, f"/api/graph_export: {r.status_code}"
    data = r.json()
    assert isinstance(data, dict), "graph_export must return JSON object"


@pytest.mark.parametrize("max_nodes", [8, 5000])
def test_graph_export_edges_are_induced_by_nodes(live_client, sample_workspace, max_nodes):
    """Every exported edge must name two exported nodes, at any `max_nodes`.

    The route selected `symbols LIMIT ?` and `edges LIMIT ?` independently, so nothing tied the
    two together. Measured 2026-08-01 at the dashboard's `max_nodes=2000`: 51.3% of exported
    edges dangled fleet-wide and **100%** on a 136-member federation, which returned 2,000 nodes
    and 2,000 edges with not one edge joining two of them. The old assertion here — 200 and
    `isinstance(dict)` — passes against an empty object and could not witness any of it.

    The federation root is the subject on purpose: the per-member budget was applied and then the
    concatenation was truncated a second time, so the multi-store path is where the rate hit 100%.
    `max_nodes=8` forces that truncation; 5,000 checks the fixture is not merely too small to cap.

    The uniqueness assertions are the second half of the same defect, and this fixture is the
    reason they are here: `expand_federation` yields the root *and* its members, the root's
    directory contains them, so every symbol under a member is indexed twice. First run returned
    194 node rows for 98 symbols. `symbol_id` hashes the absolute path, so equal ids are the same
    symbol — a duplicate row is a duplicate, never two symbols that collided.
    """
    r = live_client.get(
        f"/api/graph_export?project={sample_workspace.fed_root}&max_nodes={max_nodes}")
    assert r.status_code == 200, f"/api/graph_export: {r.status_code}"
    data = r.json()
    ids = [n["id"] for n in data["nodes"]]
    assert len(ids) <= max_nodes, f"exported {len(ids)} nodes over the {max_nodes} cap"
    assert len(set(ids)) == len(ids), (
        f"{len(ids) - len(set(ids))} of {len(ids)} exported node rows repeat an id already "
        f"exported by an overlapping federation member")
    pairs = [(e["source_id"], e["target_id"]) for e in data["edges"]]
    assert len(set(pairs)) == len(pairs), (
        f"{len(pairs) - len(set(pairs))} of {len(pairs)} exported edges repeat a "
        f"(source_id, target_id) already exported by an overlapping federation member")
    # Non-vacuity: an empty edge list satisfies the induced property for free, and this fixture
    # is three Go services that call across their own files. If this goes red the fixture stopped
    # producing edges and the assertion below stopped meaning anything.
    assert data["edges"], f"no edges exported at max_nodes={max_nodes} — assertion would be vacuous"
    present = set(ids)
    dangling = [e for e in data["edges"]
                if e["source_id"] not in present or e["target_id"] not in present]
    assert not dangling, (
        f"{len(dangling)} of {len(data['edges'])} exported edges name a node that was not "
        f"exported (max_nodes={max_nodes})")


def test_api_storage_health(live_client):
    r = live_client.get("/api/storage_health")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


# Assert one status code, never `in (200, 404)`. Six route tests here accepted a pair like that
# and so stayed green whether their route existed or not — including after it was deleted. A
# route's absence is asserted in test_server.py::test_e7_trimmed_http_surface, which requires a
# hard 404/405; a route's presence is asserted here, with a hard 200.
