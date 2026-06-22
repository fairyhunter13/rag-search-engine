"""Live e2e tests for information-hierarchy quality gate + federation-global L3 (HQ1-HQ7).

Research: arXiv 2501.07025 (composite quality signal), 2606.02019 (federation invariants),
2603.05207 (k-core deferred; recursive Leiden rejected). No mocks; GPU-only.
"""
from __future__ import annotations

import asyncio
import inspect
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opencode_search.core.config import project_graph_db
from opencode_search.core.registry import list_projects
from opencode_search.daemon.federation import expand_federation
from opencode_search.graph.store import GraphStore

pytestmark = pytest.mark.live

_OSE = str(Path(__file__).parents[3])


def _fedroot() -> str | None:
    return next((p.path for p in list_projects() if p.enabled and len(expand_federation(p.path)) > 1), None)


def test_partition_quality_on_ose():
    """HQ1: partition_quality returns valid metrics; OSE is non-degenerate."""
    from opencode_search.graph.quality import partition_quality
    gs = GraphStore(project_graph_db(_OSE))
    try:
        q = partition_quality(gs)
    finally:
        gs.close()
    assert q["n_l1"] > 0
    assert 0.0 <= q["coverage"] <= 1.0
    assert 0.0 <= q["singleton_ratio"] < 1.0
    assert not q["degenerate"], f"OSE flagged degenerate: {q}"


def test_degenerate_fires_on_all_singleton_graph(tmp_path):
    """HQ2: degenerate=True when singleton_ratio >= 0.60."""
    from opencode_search.graph.quality import partition_quality
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


def test_status_includes_hierarchy_quality(live_client):
    """HQ3a: overview(status) exposes hierarchy_quality per member."""
    r = live_client.post("/api/overview", json={"project": _OSE, "what": "status"})
    assert r.status_code == 200, f"overview status failed: {r.text[:200]}"
    d = r.json()
    assert "hierarchy_quality" in d, f"hierarchy_quality missing: {list(d.keys())}"
    assert "degenerate" in d["hierarchy_quality"]
    for m in d.get("members", []):
        assert "hierarchy_quality" in m, f"member {m.get('path','?')} missing hierarchy_quality"


def test_kb_state_demoted_when_degenerate(safe_tmp_path):
    """HQ3b: degenerate partition demotes kb_state to 'searchable'."""
    import json

    from opencode_search.core.config import ProjectEntry, project_vector_db
    from opencode_search.core.registry import remove_project, upsert_project
    from opencode_search.index.store import VectorStore
    from opencode_search.server._overview import handle_overview

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


def test_hierarchy_includes_federation_domains_and_quality(live_client):
    """HQ4: overview(hierarchy) on a federated root exposes federation_domains + quality."""
    root = _fedroot()
    if not root:
        pytest.skip("no federated root registered")
    r = live_client.post("/api/overview", json={"project": root, "what": "hierarchy"})
    assert r.status_code == 200, f"overview hierarchy failed: {r.text[:200]}"
    d = r.json()
    assert "hierarchy" in d and "quality" in d and "federation_domains" in d
    assert "n_l1" in d["quality"]


def test_federation_hierarchy_creates_no_edges():
    """HQ5a: build_federation_hierarchy must not create edges in root graph.db (HR4)."""
    from opencode_search.kb.federation_hierarchy import build_federation_hierarchy
    root = _fedroot()
    if not root:
        pytest.skip("no federated root registered")
    prev = os.environ.get("OSE_WIKI_LLM")
    os.environ["OSE_WIKI_LLM"] = "0"
    try:
        build_federation_hierarchy(root)
    finally:
        if prev is None:
            os.environ.pop("OSE_WIKI_LLM", None)
        else:
            os.environ["OSE_WIKI_LLM"] = prev
    gs = GraphStore(project_graph_db(root))
    try:
        assert gs.edge_count() == 0, f"must not add edges (HR4); got {gs.edge_count()}"
    finally:
        gs.close()


def test_federation_hierarchy_deterministic_with_llm_off():
    """HQ5b: two builds with OSE_WIKI_LLM=0 produce identical L3 rows."""
    from opencode_search.kb.federation_hierarchy import build_federation_hierarchy
    root = _fedroot()
    if not root:
        pytest.skip("no federated root registered")
    prev = os.environ.get("OSE_WIKI_LLM")
    os.environ["OSE_WIKI_LLM"] = "0"
    try:
        def _rows():
            gs = GraphStore(project_graph_db(root))
            try:
                return gs._con.execute(
                    "SELECT id,title,summary,member_count FROM communities WHERE level>=3 ORDER BY id"
                ).fetchall()
            finally:
                gs.close()
        build_federation_hierarchy(root)
        r1 = _rows()
        build_federation_hierarchy(root)
        r2 = _rows()
    finally:
        if prev is None:
            os.environ.pop("OSE_WIKI_LLM", None)
        else:
            os.environ["OSE_WIKI_LLM"] = prev
    assert r1 == r2, f"non-deterministic with OSE_WIKI_LLM=0: {r1[:3]} vs {r2[:3]}"


def test_federation_hierarchy_never_reads_symbols():
    """HQ6: source-guard — no FROM symbols in federation_hierarchy.py (token frugality)."""
    from opencode_search.kb import federation_hierarchy
    src = inspect.getsource(federation_hierarchy)
    assert "FROM symbols" not in src, "must not read FROM symbols (roll-up from L2 summaries only)"
    assert "level=2" in src or "level>=2" in src, "must read L2 communities as roll-up input"


@pytest.mark.slow
def test_global_ask_surfaces_l3_federation_domain(live_client):
    """HQ7: ask(scope=global) context references an L3 federation-domain title.

    Builds L3 deterministically (OSE_WIKI_LLM=0) if absent so this never skips on a fresh root.
    """
    from opencode_search.kb.federation_hierarchy import build_federation_hierarchy

    root = _fedroot()
    if not root:
        pytest.skip("no federated root registered")
    prev = os.environ.get("OSE_WIKI_LLM")
    os.environ["OSE_WIKI_LLM"] = "0"
    try:
        build_federation_hierarchy(root)
    finally:
        if prev is None:
            os.environ.pop("OSE_WIKI_LLM", None)
        else:
            os.environ["OSE_WIKI_LLM"] = prev
    gs = GraphStore(project_graph_db(root))
    try:
        l3_titles = [r[0] for r in gs._con.execute(
            "SELECT title FROM communities WHERE level>=3 AND title IS NOT NULL AND title!=''"
        ).fetchall()]
    finally:
        gs.close()
    assert l3_titles, "build_federation_hierarchy must have written L3 rows to root graph.db"
    from opencode_search.server.mcp import ask as _mcp_ask
    ctx = asyncio.run(_mcp_ask("What is the overall cross-service architecture?", root, "global"))
    assert any(t.lower() in ctx.lower() for t in l3_titles), (
        f"ask(scope=global) did not mention any L3 domain.\nL3: {l3_titles[:5]}\nCtx: {ctx[:300]}"
    )
