"""Live e2e: L3 member_count reconciliation invariants A1-A7 (no mocks)."""
from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path

import pytest

from opencode_search.core.config import project_graph_db
from opencode_search.core.registry import list_projects
from opencode_search.daemon.federation import expand_federation, federated_map
from opencode_search.graph.store import GraphStore

pytestmark = pytest.mark.live

_Q = ("SELECT title, COALESCE(semantic_type,'domain') FROM communities "
      "WHERE level=2 AND title IS NOT NULL AND title!='' AND title NOT IN ('(leaf)')")


def _fedroot():
    return next(
        (p.path for p in list_projects()
         if p.enabled and len(expand_federation(p.path)) > 1), None)


def _recount(root):
    g: dict[str, int] = defaultdict(int)
    for _mp, rows in federated_map(root, lambda gs: gs._con.execute(_Q).fetchall()):
        for _t, st in rows:
            g[st or "domain"] += 1
    return dict(g)


def _l3(root):
    gs = GraphStore(project_graph_db(root))
    try:
        return gs._con.execute(
            "SELECT id, title, summary, member_count FROM communities WHERE level>=3 ORDER BY id"
        ).fetchall()
    finally:
        gs.close()


def _build(root):
    from opencode_search.kb.federation_hierarchy import build_federation_hierarchy
    prev = os.environ.get("OSE_WIKI_LLM")
    os.environ["OSE_WIKI_LLM"] = "0"
    try:
        build_federation_hierarchy(root)
    finally:
        if prev is None:
            os.environ.pop("OSE_WIKI_LLM", None)
        else:
            os.environ["OSE_WIKI_LLM"] = prev


@pytest.fixture(scope="module")
def recon_root():
    root = _fedroot()
    if not root:
        pytest.fail("no federated root")
    _build(root)
    yield root


def test_a1_member_count_current(recon_root):
    """A1: stored member_count == live recount for all L3 rows."""
    counts = _recount(recon_root)
    for cid, title, _s, mc in _l3(recon_root):
        lbl = title.removeprefix("Federation: ").replace(" ", "_").lower()
        exp = counts.get(lbl) or next(
            (v for k, v in counts.items() if k.lower() == lbl), None)
        if exp is None:
            continue
        assert mc == exp, f"L3 {cid} ({title!r}) stored={mc} recount={exp}"


def test_a2_corrects_sentinel_even_fresh(recon_root):
    """A2: build fixes poisoned member_count even when db is <1800s old."""
    gs = GraphStore(project_graph_db(recon_root))
    try:
        row = gs._con.execute("SELECT id FROM communities WHERE level>=3 LIMIT 1").fetchone()
        if not row:
            pytest.fail("no L3 rows")
        cid = row[0]
        gs._con.execute("UPDATE communities SET member_count=99999 WHERE id=?", (cid,))
        gs.commit()
    finally:
        gs.close()
    _build(recon_root)
    gs = GraphStore(project_graph_db(recon_root))
    try:
        val = gs._con.execute(
            "SELECT member_count FROM communities WHERE id=?", (cid,)).fetchone()[0]
    finally:
        gs.close()
    assert val != 99999, "sentinel not corrected"


def test_a3_fresh_reuses_summary_recomputes_count(recon_root):
    """A3: fresh rebuild reuses summary, recomputes member_count."""
    gs = GraphStore(project_graph_db(recon_root))
    try:
        row = gs._con.execute("SELECT id FROM communities WHERE level>=3 LIMIT 1").fetchone()
        if not row:
            pytest.fail("no L3 rows")
        cid = row[0]
        gs._con.execute(
            "UPDATE communities SET summary='SENTINEL_A3', member_count=88888 WHERE id=?",
            (cid,))
        gs.commit()
    finally:
        gs.close()
    _build(recon_root)
    gs = GraphStore(project_graph_db(recon_root))
    try:
        s, mc = gs._con.execute(
            "SELECT summary, member_count FROM communities WHERE id=?", (cid,)).fetchone()
    finally:
        gs.close()
    assert s == "SENTINEL_A3", "fresh: must reuse summary"
    assert mc != 88888, "fresh: must recompute member_count"


def test_a6_conservation(recon_root):
    """A6: Σ L3 member_count == total live member-L2 communities."""
    t_l3 = sum(mc for _c, _t, _s, mc in _l3(recon_root) if mc)
    t_live = sum(_recount(recon_root).values())
    assert t_l3 == t_live, f"Σ L3={t_l3} live={t_live}"


def test_a7_standalone_returns_zero(safe_tmp_path):
    """A7: non-federated project → build returns 0."""
    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.registry import remove_project, upsert_project
    from opencode_search.kb.federation_hierarchy import build_federation_hierarchy
    proj = str(safe_tmp_path / "solo")
    Path(proj).mkdir()
    upsert_project(ProjectEntry(path=proj, enabled=True))
    try:
        assert build_federation_hierarchy(proj) == 0
    finally:
        remove_project(proj)
