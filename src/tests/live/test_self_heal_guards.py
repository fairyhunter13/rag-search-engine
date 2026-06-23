"""Self-healing guards: M3 (L2 coarseness) + M4 (edge-sparse completeness) + T6 cross-project.

T4  — over-granular L2 is collapsed by _enrich_project (M3 guard)
T5  — edge-sparse project gets a directory-based L2 (M4 fix, no more empty hierarchy)
T6  — cross-project: every enabled non-federation project satisfies 1 ≤ n_l2 ≤ 2√n_l1
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


@pytest.fixture()
def _proj(tmp_path):
    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.registry import remove_project, upsert_project
    proj = str(tmp_path)
    upsert_project(ProjectEntry(path=proj, enabled=True))
    yield proj
    remove_project(proj)


# ── T4: M3 coarseness guard heals over-granular L2 ──────────────────────────

@pytest.mark.slow
def test_over_granular_l2_collapsed(_proj):
    """T4: _enrich_project collapses over-granular L2 to ≤ 2×round(√n_l1)."""
    from pathlib import Path

    from opencode_search.core.config import project_graph_db
    from opencode_search.daemon.sweeps import _enrich_project, _index_project
    from opencode_search.graph.store import GraphStore

    proj = _proj
    p = Path(proj)
    for i in range(10):
        (p / f"m{i}.py").write_text(f"def fn{i}():\n    fn{(i+1)%10}()\n")
    _index_project(proj)

    db = project_graph_db(proj)
    gs = GraphStore(db)
    try:
        n_l1 = gs._con.execute("SELECT COUNT(*) FROM communities WHERE level=1").fetchone()[0]
        # Artificially inject many over-granular L2 rows.
        gs._con.execute("DELETE FROM communities WHERE level>=2")
        for k in range(n_l1 + 5):
            gs._con.execute(
                "INSERT OR REPLACE INTO communities (id, level, title, summary, member_count) "
                "VALUES (?, 2, 'D', 'x', 1)", (10000 + k,)
            )
        gs.commit()
        n_l2_before = gs._con.execute("SELECT COUNT(*) FROM communities WHERE level>=2").fetchone()[0]
        assert n_l2_before > 2 * round(n_l1 ** 0.5), "test setup: L2 must be over-granular"
    finally:
        gs.close()

    _enrich_project(proj)

    gs2 = GraphStore(db)
    try:
        n_l1_after = gs2._con.execute("SELECT COUNT(*) FROM communities WHERE level=1").fetchone()[0]
        n_l2_after = gs2._con.execute("SELECT COUNT(*) FROM communities WHERE level>=2").fetchone()[0]
        target = 2 * round(n_l1_after ** 0.5)
        assert n_l2_after <= target, (
            f"_enrich_project must collapse over-granular L2: got {n_l2_after} > {target}"
        )
    finally:
        gs2.close()


# ── T5: M4 edge-sparse project gets a directory-based L2 ────────────────────

@pytest.mark.slow
def test_edge_sparse_project_gets_l2(_proj):
    """T5: a project with no call edges gets a directory-based L2, not an empty hierarchy."""
    from pathlib import Path

    from opencode_search.core.config import project_graph_db
    from opencode_search.daemon.sweeps import _enrich_project, _index_project
    from opencode_search.graph.store import GraphStore

    proj = _proj
    p = Path(proj)
    # Two directories, files with standalone functions (no cross-calls → no edges).
    (p / "pkg_a").mkdir()
    (p / "pkg_b").mkdir()
    for i in range(3):
        (p / "pkg_a" / f"a{i}.py").write_text(f"def a{i}():\n    pass\n")
        (p / "pkg_b" / f"b{i}.py").write_text(f"def b{i}():\n    pass\n")
    _index_project(proj)
    _enrich_project(proj)

    db = project_graph_db(proj)
    gs = GraphStore(db)
    try:
        n_l1 = gs._con.execute("SELECT COUNT(*) FROM communities WHERE level=1").fetchone()[0]
        n_l2 = gs._con.execute("SELECT COUNT(*) FROM communities WHERE level>=2").fetchone()[0]
        assert n_l1 >= 2, f"test setup: need ≥2 L1 communities, got {n_l1}"
        assert n_l2 >= 1, (
            f"edge-sparse project must get a directory-based L2 (M4), got n_l2={n_l2} for n_l1={n_l1}"
        )
    finally:
        gs.close()


# ── T6: cross-project L2 coarseness lock (real data) ─────────────────────────

@pytest.mark.slow
def test_cross_project_l2_coarseness():
    """T6: every enabled non-federation project satisfies 1 ≤ n_l2 ≤ 2×round(√n_l1).

    Catches both over-granular L2 (#1) and empty hierarchy (#4).
    Run after a reconcile pass so all stale projects are healed.
    """
    import math

    from opencode_search.core.config import project_graph_db
    from opencode_search.core.registry import list_projects
    from opencode_search.graph.quality import partition_quality
    from opencode_search.graph.store import GraphStore

    failures: list[str] = []
    for entry in list_projects():
        if not entry.enabled or entry.federation:
            continue
        gdb = project_graph_db(entry.path)
        if not gdb.exists():
            continue
        gs = GraphStore(gdb)
        try:
            q = partition_quality(gs)
        finally:
            gs.close()
        n_l1, n_l2 = q["n_l1"], q["n_l2"]
        if n_l1 < 4:
            continue  # too small for a meaningful coarseness check
        target = 2 * round(math.sqrt(n_l1))
        name = entry.path.split("/")[-1]
        if not (1 <= n_l2 <= target):
            failures.append(f"{name}: n_l1={n_l1} n_l2={n_l2} target={target}")
    if failures:
        pytest.fail(
            "Cross-project L2 coarseness violated (M3/M4 — run reconcile_projects() first):\n"
            + "\n".join(failures)
        )
