"""Live e2e tests for partition quality gate (HQ1-HQ3).

No mocks; GPU-only. L2/L3 hierarchy removed (WS-B): only flat-L1 partition quality tests remain.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from rag_search.core.config import project_graph_db
from rag_search.graph.store import GraphStore
from tests.live._sample_workspace import SampleWorkspace

pytestmark = pytest.mark.live


def test_partition_quality_on_sample(sample_workspace: SampleWorkspace):
    """HQ1: partition_quality returns structurally valid metrics for a sample project.

    Uses promo-svc (7 L1 communities) so results are deterministic and machine-agnostic.
    HQ2 proves the degenerate-detection mechanism works on a synthetic graph.
    """
    from rag_search.graph.quality import partition_quality
    gs = GraphStore(project_graph_db(sample_workspace.promo))
    try:
        q = partition_quality(gs)
    finally:
        gs.close()
    assert q["n_l1"] > 0, "promo-svc must have at least one L1 community"
    assert 0.0 <= q["coverage"] <= 1.0, f"coverage out of range: {q['coverage']}"
    assert 0.0 <= q["singleton_ratio"] <= 1.0, f"singleton_ratio out of range: {q['singleton_ratio']}"
    assert isinstance(q["degenerate"], bool), "degenerate must be a bool"
    assert isinstance(q["modularity_q"], float), "modularity_q must be a float"


def test_edge_free_graph_not_degenerate(tmp_path):
    """DQ1: edge-free graph (ec=0) → degenerate=False regardless of singleton_ratio (HR20).

    An edge-free project structurally cannot form non-singleton communities via detection
    (Leiden requires edges); all clauses now require ec>0 so the gate is skipped entirely.
    """
    from rag_search.graph.quality import partition_quality
    gs = GraphStore(tmp_path / "edgefree.db")
    try:
        for i in range(7):
            gs.upsert_symbol(f"s{i}", f"fn{i}", f"fn{i}", "function", "a.py", i+1, i+2, "python")
            gs.upsert_community(i, level=1, title=f"C{i}", summary="", member_count=1)
            gs._con.execute("UPDATE symbols SET community_id=? WHERE sid=?", (i, f"s{i}"))
        # NO edges — exactly like domain-calloff (7 symbols, 0 edges)
        gs.commit()
        q = partition_quality(gs)
    finally:
        gs.close()
    assert q["singleton_ratio"] == 1.0, f"expected singleton_ratio=1.0 for all-singleton, got {q}"
    assert not q["degenerate"], (
        f"edge-free project must NOT be degenerate (ec=0 exempts all clauses per HR20): {q}"
    )


def test_degenerate_fires_on_all_singleton_graph(tmp_path):
    """HQ2: degenerate=True when singleton_ratio >= 0.60 AND edges exist (ec>0)."""
    from rag_search.graph.quality import partition_quality
    gs = GraphStore(tmp_path / "g.db")
    try:
        for i in range(4):
            gs.upsert_symbol(f"s{i}", f"fn{i}", f"fn{i}", "function", "a.py", i+1, i+2, "python")
            gs.upsert_community(i, level=1, title=f"C{i}", summary="", member_count=1)
            gs._con.execute("UPDATE symbols SET community_id=? WHERE sid=?", (i, f"s{i}"))
        gs.upsert_edge("s0", "s1")
        gs.upsert_edge("s2", "s3")
        gs.commit()
        q = partition_quality(gs)
    finally:
        gs.close()
    assert q["degenerate"], f"all-singleton graph must be degenerate: {q}"


def test_status_includes_hierarchy_quality(live_client, sample_workspace: SampleWorkspace):
    """HQ3a: overview(status) exposes hierarchy_quality per member."""
    r = live_client.post("/api/overview", json={"project": sample_workspace.promo, "what": "status"})
    assert r.status_code == 200, f"overview status failed: {r.text[:200]}"
    d = r.json()
    assert "hierarchy_quality" in d, f"hierarchy_quality missing: {list(d.keys())}"
    assert "degenerate" in d["hierarchy_quality"]
    for m in d.get("members", []):
        assert "hierarchy_quality" in m, f"member {m.get('path','?')} missing hierarchy_quality"


def test_kb_state_demoted_when_degenerate(safe_tmp_path):
    """HQ3b: degenerate partition demotes kb_state to 'searchable'."""
    import json

    from rag_search.core.config import ProjectEntry, project_vector_db
    from rag_search.core.registry import remove_project, upsert_project
    from rag_search.index.store import VectorStore
    from rag_search.server._overview import handle_overview

    proj = str(safe_tmp_path)
    VectorStore(project_vector_db(proj)).close()
    upsert_project(ProjectEntry(path=proj, enabled=True, indexed_at=datetime.now(UTC).isoformat()))
    try:
        gs = GraphStore(project_graph_db(proj))
        try:
            for i in range(4):
                gs.upsert_symbol(f"s{i}", f"fn{i}", f"fn{i}", "function", "a.py", i+1, i+2, "python")
                gs.upsert_community(i, level=1, title=f"C{i}", summary=f"s{i}", member_count=1)
                gs._con.execute("UPDATE symbols SET community_id=? WHERE sid=?", (i, f"s{i}"))
            gs.upsert_edge("s0", "s1")
            gs.upsert_edge("s2", "s3")
            gs.commit()
        finally:
            gs.close()
        result = json.loads(handle_overview(proj, "status"))
        assert result.get("kb_state") == "searchable", f"expected 'searchable'; got {result.get('kb_state')!r}"
        assert result["hierarchy_quality"]["degenerate"] is True
    finally:
        remove_project(proj)


