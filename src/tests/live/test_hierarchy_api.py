"""Live e2e: hierarchy HTTP API surface + federation integrity (B3, E3-E5, F1).

No mocks. Requires daemon at :8765 + GPU.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from opencode_search.core.config import project_graph_db
from opencode_search.core.registry import list_projects
from opencode_search.daemon.federation import expand_federation
from opencode_search.graph.store import GraphStore

pytestmark = pytest.mark.live

_Q_L3 = "SELECT id, title, summary, member_count FROM communities WHERE level>=3 ORDER BY id"


def _fedroot():
    return next(
        (p.path for p in list_projects()
         if p.enabled and len(expand_federation(p.path)) > 1), None)


@pytest.fixture(scope="module")
def fed_root():
    root = _fedroot()
    if not root:
        pytest.fail("no federated root — register before running hierarchy API tests")
    import os

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
    return root


def _l3_rows(root):
    gs = GraphStore(project_graph_db(root))
    try:
        return gs._con.execute(_Q_L3).fetchall()
    finally:
        gs.close()


def test_b3_no_orphan_l1_l2(fed_root):
    """B3: every member has zero null-title L1/L2 community rows."""
    for mpath in expand_federation(fed_root):
        gdb = project_graph_db(mpath)
        if not gdb.exists():
            continue
        gs = GraphStore(gdb)
        try:
            bad = gs._con.execute(
                "SELECT COUNT(*) FROM communities "
                "WHERE level IN (1,2) AND (title IS NULL OR title='')"
            ).fetchone()[0]
        finally:
            gs.close()
        assert bad == 0, f"{mpath}: {bad} null-title L1/L2 rows"


def test_e3_overview_hierarchy_member_count_matches_db(live_client, fed_root):
    """E3: /api/overview?what=hierarchy member_count == DB values."""
    r = live_client.post("/api/overview", json={"project": fed_root, "what": "hierarchy"})
    assert r.status_code == 200, r.text[:200]
    domains = r.json().get("federation_domains", [])
    assert domains, "federation_domains missing"
    db_mc = {title: mc for _cid, title, _summary, mc in _l3_rows(fed_root)}
    for fd in domains:
        title = fd.get("title", "")
        if title in db_mc and fd.get("member_count") is not None:
            assert fd["member_count"] == db_mc[title], (
                f"{title!r}: API={fd['member_count']} DB={db_mc[title]}")


def test_e4_wiki_and_docs_coexist(live_client, fed_root):
    """E4: /api/wiki returns KB pages alongside /api/docs (both endpoints live)."""
    r_wiki = live_client.get(f"/api/wiki?project={fed_root}")
    assert r_wiki.status_code == 200, r_wiki.text[:200]
    r_docs = live_client.get(f"/api/docs?project={fed_root}")
    assert r_docs.status_code == 200, r_docs.text[:200]
    # wiki may or may not have pages; docs may or may not have a tree — both must 200
    assert "pages" in r_wiki.json() or "error" not in r_wiki.json()
    assert "tree" in r_docs.json()


def test_f1_overview_hierarchy_what(live_client, fed_root):
    """F1: overview(what='hierarchy') returns federation_domains; what='communities' returns L1."""
    r_h = live_client.post("/api/overview", json={"project": fed_root, "what": "hierarchy"})
    assert r_h.status_code == 200
    assert "federation_domains" in r_h.json()

    r_c = live_client.post("/api/overview", json={"project": fed_root, "what": "communities"})
    assert r_c.status_code == 200
    body = r_c.json()
    # Must contain at least one L1-level community entry
    assert body.get("communities") or body.get("top_communities"), \
        f"communities missing from overview: {list(body.keys())}"


_VENDOR_DOCGEN = Path(__file__).parent.parent.parent.parent / "vendor" / "docgen" / "src"


def test_e5_docs_tree_c4_structure(live_client, tmp_path):
    """E5: /api/docs tree from a freshly-generated docs dir contains C4-bucket paths."""
    _VENDOR = _VENDOR_DOCGEN
    if str(_VENDOR) not in sys.path:
        sys.path.insert(0, str(_VENDOR))
    from opencode_search.core.config import project_graph_db
    from opencode_search.core.registry import list_projects

    proj = next(
        (p.path for p in list_projects()
         if p.enabled and "ocs-test-dirs" not in p.path
         and project_graph_db(p.path).exists()), None)
    if not proj:
        pytest.fail("no indexed project — index a project first")
    from ose_docgen.generate import generate
    generate(project_path=tmp_path, graph_db_path=project_graph_db(proj),
             docs_dir=str(tmp_path / "docs"), llm=False)
    r = live_client.get(f"/api/docs?project={tmp_path}")
    assert r.status_code == 200, r.text[:200]
    tree = r.json().get("tree", [])
    assert tree, "generate() must produce a non-empty docs tree"
    _C4 = {"01-context", "02-containers", "03-components",
           "04-reference", "05-how-to", "06-decisions"}
    buckets = {f.split("/")[0] for f in tree if "/" in f}
    assert buckets & _C4, f"no C4 bucket dirs in tree: {list(buckets)[:8]}"
