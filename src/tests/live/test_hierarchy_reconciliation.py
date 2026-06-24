"""Live e2e: L3 member_count reconciliation invariants A1-A7 (no mocks).

All tests operate on a SYNTHETIC isolated federated root so no live/production graph.db
is ever mutated.  The synthetic root has two members with distinct L2 semantic_types
(api, data) — this also exercises Fix 2 (multi-theme L3 via assign_l2_semantic_types).
"""
from __future__ import annotations

import os
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path

import pytest

from opencode_search.core.config import ProjectEntry, project_graph_db
from opencode_search.core.registry import list_projects, remove_project, upsert_project
from opencode_search.daemon.federation import federated_map
from opencode_search.graph.store import GraphStore

pytestmark = pytest.mark.live

_Q = ("SELECT title, COALESCE(semantic_type,'domain') FROM communities "
      "WHERE level=2 AND title IS NOT NULL AND title!='' AND title NOT IN ('(leaf)')")

_SAFE_BASE = Path.home() / ".local" / "share" / "ocs-test-dirs"


def _make_member(parent: Path, name: str, stype: str) -> str:
    """Create a minimal member dir with one L2 community of the given semantic_type."""
    d = parent / name
    d.mkdir(parents=True)
    gdb = project_graph_db(str(d))
    gdb.parent.mkdir(parents=True, exist_ok=True)
    gs = GraphStore(gdb)
    try:
        gs.upsert_community(1, level=2, title=f"{name}-domain",
                            summary="test domain", member_count=3, narrated=1)
        gs._con.execute("UPDATE communities SET semantic_type=? WHERE id=1", (stype,))
        gs.commit()
    finally:
        gs.close()
    upsert_project(ProjectEntry(path=str(d), enabled=True))
    return str(d)


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
    """Synthetic isolated federated root with 2 members (api + data types)."""
    _SAFE_BASE.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(dir=_SAFE_BASE))
    root = str(base / "root")
    Path(root).mkdir()
    m1 = _make_member(base, "m1", "api")
    m2 = _make_member(base, "m2", "data")
    upsert_project(ProjectEntry(path=root, enabled=True, federation=[m1, m2]))
    # Initialise the root graph.db (build_federation_hierarchy requires it to exist).
    gdb = project_graph_db(root)
    gdb.parent.mkdir(parents=True, exist_ok=True)
    gs_root = GraphStore(gdb)
    gs_root.close()
    _build(root)
    yield root
    for p in (root, m1, m2):
        remove_project(p)
    shutil.rmtree(base, ignore_errors=True)


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
    from opencode_search.kb.federation_hierarchy import build_federation_hierarchy
    proj = str(safe_tmp_path / "solo")
    Path(proj).mkdir()
    upsert_project(ProjectEntry(path=proj, enabled=True))
    try:
        assert build_federation_hierarchy(proj) == 0
    finally:
        remove_project(proj)


def test_no_sentinel_in_live_federated_roots():
    """Guard: no production federated root should have a SENTINEL_-prefixed L3 summary."""
    for entry in list_projects():
        if not entry.enabled or not entry.federation or "ocs-test-dirs" in entry.path:
            continue
        gdb = project_graph_db(entry.path)
        if not gdb.exists():
            continue
        gs = GraphStore(gdb)
        try:
            bad = gs._con.execute(
                "SELECT id, summary FROM communities "
                "WHERE level>=3 AND summary LIKE 'SENTINEL_%'"
            ).fetchall()
        finally:
            gs.close()
        assert not bad, f"{entry.path}: L3 rows with SENTINEL_ summary: {bad}"
