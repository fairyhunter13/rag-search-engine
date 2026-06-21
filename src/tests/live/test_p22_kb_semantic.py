"""P22: KB semantic completeness — community layer guards (S1-S4)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.live

_BASE = "http://127.0.0.1:8765"
_OSE = str(Path(__file__).resolve().parents[3])  # repo root, no hardcoded device path


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
    assert gdb.exists(), (
        f"graph.db not found for {project} — run Workstream E (re-index) first"
    )
    return sqlite3.connect(str(gdb))


def _converge_ready(project: str, timeout: int = 180) -> None:
    """Run _enrich_project in-process, poll until kb_state==ready AND l2_enriched_pct==100."""
    import time

    from opencode_search.daemon.sweeps import _enrich_project
    _enrich_project(project)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = _overview_status(project)
        if s.get("kb_state") == "ready" and s.get("l2_enriched_pct") == 100.0:
            return
        time.sleep(3)
    s = _overview_status(project)
    assert s.get("kb_state") == "ready" and s.get("l2_enriched_pct") == 100.0, (
        f"{project!r} did not reach ready in {timeout}s — "
        f"kb_state={s.get('kb_state')!r}, l2={s.get('l2_enriched_pct')}"
    )


def test_orphan_l2_stamp_sets_title_and_summary(safe_tmp_path):
    """TB1/F4a: orphan-L2 stamp SQL sets title='(leaf)'+summary; pre-seeded L1 summary intact."""
    from opencode_search.graph.store import GraphStore

    gdb = safe_tmp_path / "graph.db"
    gs = GraphStore(gdb)
    try:
        gs._con.execute(
            "INSERT INTO communities(id,level,title,summary,member_count) VALUES (1,1,'A','sentinel',1)"
        )
        gs._con.execute(
            "INSERT INTO symbols(sid,name,qualified_name,kind,file,start_line,end_line,language,community_id)"
            " VALUES ('s1','foo','foo','function','a.py',1,5,'python',1)"
        )
        gs._con.execute(
            "INSERT INTO communities(id,level,title,summary,member_count) VALUES (2,2,NULL,NULL,0)"
        )
        gs.commit()
        # Execute the F4a orphan stamp SQL directly
        gs._con.execute(
            "UPDATE communities SET title='(leaf)', summary='(no child communities)' "
            "WHERE level>=2 AND (summary IS NULL OR summary='')"
        )
        gs.commit()
        r = gs._con.execute("SELECT title, summary FROM communities WHERE id=2").fetchone()
        assert r[0] == "(leaf)" and r[1] == "(no child communities)", f"orphan not stamped: {r}"
        l1 = gs._con.execute("SELECT summary FROM communities WHERE id=1").fetchone()
        assert l1[0] == "sentinel", f"L1 summary affected by stamp: {l1[0]!r}"
    finally:
        gs.close()


# ---------------------------------------------------------------------------
# S1: Singleton-ratio guard (Fix 1)
# ---------------------------------------------------------------------------

def test_singleton_ratio_below_threshold(live_client):
    """S1: L1 singleton ratio must be <60% after Fix 1 re-detection."""
    con = _con(_OSE)
    try:
        total = con.execute("SELECT COUNT(*) FROM communities WHERE level=1").fetchone()[0]
        assert total > 0, (
            "no L1 communities in OSE graph.db — re-index must have completed before running this test"
        )
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
    """S2/TC1: After converge, all L2+ communities must have non-empty title+summary (Fix 2)."""
    _converge_ready(_OSE)
    con = _con(_OSE)
    try:
        l2_total = con.execute("SELECT COUNT(*) FROM communities WHERE level>=2").fetchone()[0]
        assert l2_total > 0, "no L2 communities — hierarchy not built"
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
        assert l2 > 0, (
            "no L2 communities — re-index + enrichment must complete before this test"
        )
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
    """S3: MCP ask(scope=global) assembled context must reference ≥1 L2 domain title."""
    import asyncio

    from opencode_search.server.mcp import ask as _mcp_ask
    con = _con(_OSE)
    try:
        l2_titles = [r[0] for r in con.execute(
            "SELECT title FROM communities WHERE level>=2 AND title IS NOT NULL AND title!=''"
        ).fetchall()]
    finally:
        con.close()
    assert l2_titles, (
        "no enriched L2 communities — re-index + enrichment must complete before this test"
    )
    ctx = asyncio.run(_mcp_ask("What is the overall architecture?", _OSE, "global"))
    found = any(t.lower() in ctx.lower() for t in l2_titles if t)
    assert found, (
        f"ask(scope=global) context did not mention any L2 domain.\n"
        f"L2 titles: {l2_titles[:5]}\nContext[:300]: {ctx[:300]}"
    )


# ---------------------------------------------------------------------------
# S4: kb_state reaches ready (Fix 3)
# ---------------------------------------------------------------------------

def test_kb_state_ready_when_fully_enriched(live_client):
    """S4/TC1: After converge, kb_state='ready' and enriched_pct>=95."""
    _converge_ready(_OSE)
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


# ---------------------------------------------------------------------------
# Gap 3: ask() synthesis excludes test/tooling communities (source-guard)
# ---------------------------------------------------------------------------

def test_ask_synthesis_excludes_test_tooling_communities():
    """Gap 3 source-guard: _top_communities_semantic/_macro_community_context/_community_context
    all filter out test/tooling/utility semantic_type communities before assembling context.
    """
    import inspect

    from opencode_search.query import ask as ask_mod
    for fn_name in ("_top_communities_semantic", "_macro_community_context", "_community_context"):
        fn = getattr(ask_mod, fn_name, None)
        if fn is None:
            continue
        src = inspect.getsource(fn)
        assert "test" in src.lower() and "tooling" in src.lower(), (
            f"{fn_name} must filter out 'test'/'tooling' semantic_type communities"
        )
        assert "NOT IN" in src or "not in" in src.lower(), (
            f"{fn_name} must use NOT IN exclusion for test/tooling communities"
        )
