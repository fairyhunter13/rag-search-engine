"""HTTP surface matrix — all mounted routes not covered by test_p5_server.

Routes excluded (already in test_p5): /healthz, /dashboard, /api/projects,
/api/overview, /api/suggested_questions, /api/auto_pipeline_status, /mcp.

Routes verified here — this is the *surviving* half after R0; the tier-3 rows this
docstring used to list are asserted absent by test_p5_server.py::test_e7_trimmed_http_surface:
  GET /               → 200 or redirect
  GET /api/metrics    → 200, and no LLM token accounting
  POST /api/sweeps/*  → 200
  GET /api/graph_export  → 200, node/edge structure
  GET /api/storage_health → 200
"""
from __future__ import annotations

import pytest

from tests.live._sample_workspace import SampleWorkspace

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
    """Pause then resume — both 200; leave sweeps paused (autouse fixture expects it paused)."""
    assert live_client.post("/api/sweeps/pause").status_code == 200
    assert live_client.post("/api/sweeps/resume").status_code == 200
    assert live_client.post("/api/sweeps/pause").status_code == 200  # restore paused state


# /api/events/stream left with tier 3. Its only publisher was the pipeline job runner
# (build_wiki / docgen / okf), so `publish_event` had no callers after R0 and the bus could only
# emit its own "connected"/"keepalive" frames — this test would have kept asserting the
# content-type of an empty stream. The route now joins the `deleted` list in
# test_p5_server.py::test_e7_trimmed_http_surface, which demands a hard 404/405 for it.


def test_api_graph_export(live_client, project):
    r = live_client.get(f"/api/graph_export?project={project}")
    assert r.status_code == 200, f"/api/graph_export: {r.status_code}"
    data = r.json()
    assert isinstance(data, dict), "graph_export must return JSON object"


def test_api_storage_health(live_client):
    r = live_client.get("/api/storage_health")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


# Six route tests left with tier 3: /api/wiki, /api/wiki/page, /api/wiki/export, /api/kb_health,
# /api/build_wiki and /api/process/bpmn (BPRE's process graph, whose `root_process_db` went with
# it). Five of the six accepted `in (200, 404)` or `in (400, 404)`, which is the shape of a guard
# that cannot witness its own subject: they stayed green whether the route existed or not, and
# they stayed green after R0a deleted it. Only kb_health demanded a hard 200, and that one was
# simply red. So the property is not re-pointed here — it moves to
# test_p5_server.py::test_e7_trimmed_http_surface, whose `deleted` list names all six and requires
# a hard 404/405. The `fed_root` fixture goes with process/bpmn, its only consumer.
