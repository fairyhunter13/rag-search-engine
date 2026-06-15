"""P22: KB semantic completeness — community layer guards (S1-S4)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.live

_BASE = "http://127.0.0.1:8765"
_OSE = "/home/user/git/github.com/fairyhunter13/opencode-search-engine"


def _graph_db(project: str) -> Path:
    from opencode_search.core.config import project_graph_db
    return project_graph_db(project)


def _overview_status(project: str) -> dict:
    import json
    r = requests.post(f"{_BASE}/api/overview",
                      json={"what": "status", "project_path": project}, timeout=15)
    assert r.status_code == 200
    return json.loads(r.text)


def _con(project: str):
    gdb = _graph_db(project)
    if not gdb.exists():
        pytest.skip(f"graph.db not found for {project}")
    return sqlite3.connect(str(gdb))


# ---------------------------------------------------------------------------
# S1: Singleton-ratio guard (Fix 1)
# ---------------------------------------------------------------------------

def test_singleton_ratio_below_threshold(live_client):
    """S1: L1 singleton ratio must be <60% after Fix 1 re-detection."""
    con = _con(_OSE)
    try:
        total = con.execute("SELECT COUNT(*) FROM communities WHERE level=1").fetchone()[0]
        if total == 0:
            pytest.skip("no L1 communities")
        singletons = con.execute(
            "SELECT COUNT(*) FROM communities WHERE level=1 AND member_count=1"
        ).fetchone()[0]
        ratio = singletons / total
        assert ratio < 0.60, (
            f"L1 singleton ratio {ratio:.1%} ({singletons}/{total}) ≥ 60% "
            f"— re-run detect_communities to apply Fix 1"
        )
    finally:
        con.close()


# ---------------------------------------------------------------------------
# S2: L2-summary coverage (Fix 2)
# ---------------------------------------------------------------------------

def test_l2_communities_all_enriched(live_client):
    """S2: All L2+ communities must have non-empty title+summary (Fix 2)."""
    con = _con(_OSE)
    try:
        l2_total = con.execute("SELECT COUNT(*) FROM communities WHERE level>=2").fetchone()[0]
        if l2_total == 0:
            pytest.skip("no L2 communities")
        unenriched = con.execute(
            "SELECT COUNT(*) FROM communities "
            "WHERE level>=2 AND (summary IS NULL OR summary='' OR title IS NULL OR title='')"
        ).fetchone()[0]
        assert unenriched == 0, f"{unenriched}/{l2_total} L2+ communities lack title or summary"
    finally:
        con.close()


# ---------------------------------------------------------------------------
# S2b: Coarse-resolution regression lock
# ---------------------------------------------------------------------------

def test_l2_coarse_resolution_lock():
    """S2b: L2 count must be ≤2×√n_L1 and <60% of L1 (guards against degenerate hierarchy)."""
    con = _con(_OSE)
    try:
        l1 = con.execute(
            "SELECT COUNT(DISTINCT community_id) FROM symbols WHERE community_id IS NOT NULL"
        ).fetchone()[0]
        l2 = con.execute("SELECT COUNT(*) FROM communities WHERE level>=2").fetchone()[0]
        if l2 == 0:
            pytest.skip("no L2 communities yet")
        target = max(2, round(l1 ** 0.5))
        assert l2 <= 2 * target, f"L2={l2} > 2×√L1={2*target}: hierarchy is degenerate"
        assert l2 < l1 * 0.6, f"L2={l2} ≥ 60% of L1={l1}: coarsening is insufficient"
    finally:
        con.close()


# ---------------------------------------------------------------------------
# S3: Macro view in global ask (Fix 3)
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_global_ask_includes_l2_domain(live_client):
    """S3: ask(scope=global) must reference ≥1 L2 domain title (Fix 3)."""
    con = _con(_OSE)
    try:
        l2_titles = [r[0] for r in con.execute(
            "SELECT title FROM communities WHERE level>=2 AND title IS NOT NULL AND title!=''"
        ).fetchall()]
    finally:
        con.close()
    if not l2_titles:
        pytest.skip("no enriched L2 communities yet")
    r = requests.post(f"{_BASE}/api/ask",
                      json={"query": "What is the overall architecture?",
                            "project_path": _OSE, "scope": "global"},
                      timeout=60)
    assert r.status_code == 200
    answer = r.text
    found = any(t.lower() in answer.lower() for t in l2_titles if t)
    assert found, (
        f"ask(scope=global) did not mention any L2 domain.\n"
        f"L2 titles: {l2_titles[:5]}\nAnswer[:300]: {answer[:300]}"
    )


# ---------------------------------------------------------------------------
# S4: kb_state reaches ready (Fix 3)
# ---------------------------------------------------------------------------

def test_kb_state_ready_when_fully_enriched(live_client):
    """S4: kb_state='ready' and enriched_pct>=95 when L1+L2 both enriched."""
    con = _con(_OSE)
    try:
        l2_un = con.execute(
            "SELECT COUNT(*) FROM communities WHERE level>=2 AND (summary IS NULL OR summary='')"
        ).fetchone()[0]
    finally:
        con.close()
    if l2_un > 0:
        pytest.skip(f"{l2_un} L2 communities still unenriched")
    status = _overview_status(_OSE)
    assert status.get("kb_state") == "ready", (
        f"kb_state={status.get('kb_state')!r}, pct={status.get('enriched_pct')}"
    )
    assert status.get("enriched_pct", 0) >= 95.0


def test_status_includes_level_breakdown_fields(live_client):
    """S4b: overview(status) must include l1_enriched_pct + l2_enriched_pct fields."""
    status = _overview_status(_OSE)
    for field in ("enriched_pct", "l1_enriched_pct", "l2_enriched_pct", "kb_state"):
        assert field in status, f"overview(status) missing field {field!r}"
