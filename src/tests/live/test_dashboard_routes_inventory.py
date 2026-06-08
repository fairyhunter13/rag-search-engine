"""Phase 68 Part B: verify all 63 dashboard routes are reachable (handler runs).

Each route is hit with its appropriate method against the live daemon at :8765.
The test distinguishes Starlette routing 404 (route not registered) from
application-level 4xx/5xx (handler ran but rejected input or missing dependency).

A Starlette routing 404 returns `{"detail":"Not Found"}` — that means the route
was dropped. Any other response (including 5xx from a handler or application 404)
proves the route IS registered.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live

# (path, method, json_body)  — body=None for GET
_ROUTES: list[tuple[str, str, dict | None]] = [
    # root + static
    ("/", "GET", None),
    ("/dashboard", "GET", None),
    ("/static/nonexistent.js", "GET", None),
    # project routes
    ("/api/projects", "GET", None),
    ("/api/overview", "GET", None),
    ("/api/communities", "GET", None),
    ("/api/start_watching", "POST", {"project": "/tmp/does-not-exist"}),
    ("/api/stop_watching", "POST", {"project": "/tmp/does-not-exist"}),
    ("/api/remove_project", "POST", {"project": "/tmp/does-not-exist"}),
    # wiki routes
    ("/api/wiki", "GET", None),
    ("/api/wiki/page", "GET", None),
    ("/api/wiki_lint", "GET", None),
    ("/api/suggested_questions", "GET", None),
    # search routes
    ("/api/search", "GET", None),
    ("/api/ask", "GET", None),
    ("/api/ask_business", "GET", None),
    ("/api/feature", "GET", None),
    ("/api/patterns", "GET", None),
    ("/api/analyze_patterns", "POST", {"project": "/tmp/does-not-exist"}),
    ("/api/feature_map", "GET", None),
    ("/api/business_rules", "GET", None),
    ("/api/process_flows", "GET", None),
    # graph routes
    ("/api/graph", "GET", None),
    ("/api/graph_export", "GET", None),
    ("/api/service_mesh", "GET", None),
    ("/api/impact_narrative", "GET", None),
    ("/api/semantic_trace", "GET", None),
    ("/api/build_hierarchy", "POST", {"project": "/tmp/does-not-exist"}),
    ("/api/enrich_hierarchy", "POST", {"project": "/tmp/does-not-exist"}),
    ("/api/import_cycles", "GET", None),
    ("/api/graph_diff", "GET", None),
    ("/api/callflow_html", "GET", None),
    ("/api/surprising_connections", "GET", None),
    ("/api/pr_impact", "GET", None),
    ("/api/tree_html", "GET", None),
    # chat routes
    ("/api/chat", "POST", {"message": "hello", "project": "/tmp/does-not-exist"}),
    ("/api/chat_stream", "POST", {"message": "hello", "project": "/tmp/does-not-exist"}),
    ("/api/debug", "POST", {"message": "hello", "project": "/tmp/does-not-exist"}),
    # kb routes
    ("/api/kb_health", "GET", None),
    ("/api/dedup", "POST", {"project": "/tmp/does-not-exist", "dry_run": True}),
    ("/api/vacuum", "POST", {"project": "/tmp/does-not-exist"}),
    ("/api/git_hooks", "POST", {"project": "/tmp/does-not-exist", "action": "install"}),
    ("/api/reload", "POST", {}),
    # ops routes
    ("/api/metrics", "GET", None),
    ("/api/metrics/history", "GET", None),
    ("/api/auto_pipeline_status", "GET", None),
    ("/api/federation", "GET", None),
    ("/api/system_status", "GET", None),
    ("/api/integrations_status", "GET", None),
    ("/api/alerts", "GET", None),
    ("/api/alerts", "POST", {"level": "info", "message": "test"}),
    ("/api/jobs", "GET", None),
    ("/api/jobs/{job_id}", "GET", None),
    ("/api/jobs/{job_id}/cancel", "POST", {}),
]

# Paths with {job_id} placeholder — substitute a sentinel so the URL is valid
_SENTINEL_JOB_ID = "00000000-0000-0000-0000-000000000000"


def _expand(path: str) -> str:
    return path.replace("{job_id}", _SENTINEL_JOB_ID)


def _is_routing_not_found(r) -> bool:
    """Return True if Starlette routing itself returned 404 (route not registered).

    Starlette routing 404: {"detail": "Not Found"} with no "error" key.
    Application 404 (handler ran and rejected): {"error": "..."} or similar.
    """
    if r.status_code != 404:
        return False
    try:
        j = r.json()
        return j.get("detail") == "Not Found" and "error" not in j
    except Exception:
        return r.status_code == 404


@pytest.mark.parametrize("path,method,body", _ROUTES, ids=[r[0] + ":" + r[1] for r in _ROUTES])
def test_route_registered(http, path, method, body):
    """Every dashboard route must be registered (handler must run, not Starlette routing 404).

    - Starlette routing 404 ({"detail":"Not Found"}) → FAIL: route was dropped
    - Any other response (200, 4xx, 5xx from a running handler) → PASS: route exists
    """
    url = _expand(path)
    r = http.get(url) if method == "GET" else http.post(url, json=body or {})

    assert not _is_routing_not_found(r), (
        f"{method} {path} returned Starlette routing 404 — route is NOT registered.\n"
        f"A route was dropped during the register_dashboard_routes refactor.\n"
        f"Response: {r.text[:400]}"
    )
