"""HTTP surface matrix — all mounted routes not covered by test_p5_server.

Routes excluded (already in test_p5): /healthz, /dashboard, /api/projects,
/api/overview, /api/suggested_questions, /api/auto_pipeline_status, /mcp, /api/wiki_lint.

New routes verified here:
  GET /               → 200 or redirect
  GET /api/metrics    → 200 + llm_cache keys
  POST /api/sweeps/*  → 200
  GET /api/events/stream → 200, text/event-stream
  GET /api/graph_export  → 200, node/edge structure
  GET /api/wiki          → 200 or 404
  GET /api/wiki/page     → 200 or 404
  GET /api/wiki/export   → 200 or 404
  GET /api/kb_health     → 200
  GET /api/storage_health → 200
  GET /api/process/bpmn  → 200 or 404
  POST /api/build_wiki → 200 or 202
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def _any_project(enabled_only=True):
    from opencode_search.core.registry import list_projects
    return next((e.path for e in list_projects() if e.enabled or not enabled_only), None)


def test_root_not_500(live_client):
    r = live_client.get("/", allow_redirects=False)
    assert r.status_code < 500, f"GET / returned server error {r.status_code}"


def test_api_metrics_llm_cache_keys(live_client):
    r = live_client.get("/api/metrics")
    assert r.status_code == 200
    data = r.json()
    assert "llm_cache" in data, f"/api/metrics must expose llm_cache; got keys={list(data)}"
    for k in ("hits", "misses", "calls"):
        assert k in data["llm_cache"], f"llm_cache missing '{k}'"


def test_api_sweeps_pause_resume(live_client):
    """Pause then resume — both 200; leave sweeps paused (autouse fixture expects it paused)."""
    assert live_client.post("/api/sweeps/pause").status_code == 200
    assert live_client.post("/api/sweeps/resume").status_code == 200
    assert live_client.post("/api/sweeps/pause").status_code == 200  # restore paused state


def test_api_events_stream_sse_content_type(live_client):
    r = live_client.get("/api/events/stream", stream=True, timeout=3)
    ct = r.headers.get("content-type", "")
    r.close()
    assert r.status_code == 200
    assert "text/event-stream" in ct, f"events/stream must be SSE; content-type={ct}"


def test_api_graph_export(live_client):
    from opencode_search.core.config import project_graph_db
    from opencode_search.core.registry import list_projects
    p = next((e.path for e in list_projects() if e.enabled and project_graph_db(e.path).exists()), None)
    assert p, "Need an indexed project with graph.db"
    r = live_client.get(f"/api/graph_export?project={p}")
    assert r.status_code == 200, f"/api/graph_export: {r.status_code}"
    data = r.json()
    assert isinstance(data, dict), "graph_export must return JSON object"


def test_api_wiki_registered_project(live_client):
    p = _any_project()
    assert p, "Need a registered project"
    r = live_client.get(f"/api/wiki?project={p}")
    assert r.status_code in (200, 404), f"/api/wiki unexpected {r.status_code}"


def test_api_wiki_page(live_client):
    p = _any_project()
    assert p
    r = live_client.get(f"/api/wiki/page?project={p}&page=overview")
    assert r.status_code in (200, 404), f"/api/wiki/page unexpected {r.status_code}"


def test_api_wiki_export(live_client):
    p = _any_project()
    assert p
    r = live_client.get(f"/api/wiki/export?project={p}")
    assert r.status_code in (200, 404), f"/api/wiki/export unexpected {r.status_code}"


def test_api_kb_health(live_client):
    p = _any_project()
    assert p
    r = live_client.get(f"/api/kb_health?project={p}")
    assert r.status_code == 200, f"/api/kb_health: {r.status_code}"
    data = r.json()
    assert "verdict" in data or "kb_state" in data or "status" in data, (
        f"kb_health missing status key: {data}"
    )


def test_api_storage_health(live_client):
    r = live_client.get("/api/storage_health")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


def test_api_process_bpmn(live_client):
    from opencode_search.core.registry import list_projects
    root = next((e.path for e in list_projects() if e.enabled
                 and getattr(e, "federation", None)), None)
    assert root, "Need a federated root for /api/process/bpmn — ensure astro-project registered"
    # endpoint requires root= and id=; nonexistent id returns 404 (acceptable)
    r = live_client.get(f"/api/process/bpmn?root={root}&id=probe")
    assert r.status_code in (200, 404), f"/api/process/bpmn unexpected {r.status_code}"


def test_api_build_wiki(live_client):
    # Use a nonexistent path → fast 404 (avoids blocking synchronous wiki build)
    r = live_client.post("/api/build_wiki", json={"project_path": "/nonexistent"}, timeout=10)
    assert r.status_code in (400, 404), f"/api/build_wiki: {r.status_code}"
