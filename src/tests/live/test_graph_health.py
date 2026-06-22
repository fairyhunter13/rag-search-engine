"""Graph-health guards — symbol-hollow detection and clear-on-reindex invariants.

Tests:
  GH1: GraphStore.clear() wipes symbols/edges/communities
  GH2: symbol_hollow flag fires on edge-free graph (communities>0, edges=0)
  GH3: overview(status) includes symbol_hollow field on healthy projects
  GH4: reconcile source-guard: checks edge_count() == 0 to detect stale graphs
  GH5: _index_project source-guard: calls gs.clear() before rebuild
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.live


def test_graph_store_clear_wipes_tables(safe_tmp_path):
    """GH1: GraphStore.clear() must delete symbols, edges, and communities."""
    from opencode_search.graph.community import detect_communities
    from opencode_search.graph.extractor import extract_symbols, symbol_id
    from opencode_search.graph.store import GraphStore

    gdb = safe_tmp_path / "graph.db"
    gs = GraphStore(gdb)
    try:
        fpath = safe_tmp_path / "auth.py"
        fpath.write_text("def authenticate(token): pass\ndef validate(t): return bool(t)\n")
        for sym in extract_symbols(fpath, fpath.read_text(), "python"):
            gs.upsert_symbol(symbol_id(str(fpath), sym.name, sym.start_line),
                             sym.name, sym.qualified_name, sym.kind,
                             str(fpath), sym.start_line, sym.end_line, sym.language)
        gs.commit()
        detect_communities(gs)
        assert gs.symbol_count() > 0
        assert gs.community_count() > 0
        gs.clear()
        assert gs.symbol_count() == 0, "clear() must delete all symbols"
        assert gs.edge_count() == 0, "clear() must delete all edges"
        assert gs.community_count() == 0, "clear() must delete all communities"
    finally:
        gs.close()


def test_symbol_hollow_flag_fires_on_edge_free_graph(safe_tmp_path):
    """GH2: communities>0 but edges=0 must set symbol_hollow=True in overview(status)."""
    import asyncio

    from opencode_search.core.config import ProjectEntry, project_graph_db
    from opencode_search.core.registry import remove_project, upsert_project
    from opencode_search.graph.store import GraphStore
    from opencode_search.server.mcp import overview as overview_tool

    proj = str(safe_tmp_path)
    upsert_project(ProjectEntry(path=proj, enabled=True))
    try:
        gs = GraphStore(project_graph_db(proj))
        try:
            gs._con.execute(
                "INSERT INTO symbols(sid,name,qualified_name,kind,file,start_line,end_line,language)"
                " VALUES ('s1','foo','pkg.foo','function','main.go',1,5,'go')"
            )
            gs._con.execute(
                "INSERT INTO communities(id,level,title,summary,member_count)"
                " VALUES (1,1,'Core','summarized',1)"
            )
            gs._con.execute("UPDATE symbols SET community_id=1 WHERE sid='s1'")
            gs.commit()
        finally:
            gs.close()

        result = json.loads(asyncio.run(overview_tool(proj, "status")))
        assert result.get("symbol_hollow") is True, f"edge-free graph must have symbol_hollow=True; got {result}"
        member = next((m for m in result.get("members", []) if m["path"] == proj), None)
        assert member is not None and member.get("symbol_hollow") is True
        assert member.get("edges") == 0
    finally:
        remove_project(proj)


def test_overview_status_includes_symbol_hollow_field(project_with_communities):
    """GH3: healthy project must include symbol_hollow=False in overview(status)."""
    import asyncio

    from opencode_search.server.mcp import overview as overview_tool

    result = json.loads(asyncio.run(overview_tool(project_with_communities, "status")))
    assert "symbol_hollow" in result, f"symbol_hollow missing; keys={list(result)}"
    assert result["symbol_hollow"] is False, f"healthy project must not be hollow; members={result.get('members', [])[:2]}"


def test_reconcile_treats_edge_hollow_as_needs_reindex():
    """GH4: reconcile_projects source-guard: must check edge_count() for stale detection."""
    import inspect

    from opencode_search.daemon import sweeps
    src = inspect.getsource(sweeps.reconcile_projects)
    assert "edge_count()" in src, "reconcile must check edge_count() to detect edge-hollow graphs"
    assert "community_count() > 0" in src, "reconcile edge-hollow check must require community_count() > 0"


def test_index_project_clears_graph_before_rebuild():
    """GH5: _index_project source-guard: gs.clear() must be called before upsert loop."""
    import inspect

    from opencode_search.daemon import sweeps
    src = inspect.getsource(sweeps._index_project)
    assert "gs.clear()" in src, "_index_project must call gs.clear() before upserting symbols"
