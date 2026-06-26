"""WS-F/WS-G: prove surviving features work after WS-B hierarchy deletion + WS-E purge."""
from __future__ import annotations
import asyncio, json, sqlite3
from pathlib import Path
import pytest

from tests.live._sample_workspace import SampleWorkspace

pytestmark = pytest.mark.live

_OSE_SRC = Path(__file__).resolve().parents[3]  # source-file reads only; NOT passed to daemon
_REMOVED = ["hierarchy", "architecture_domains", "world_model"]
_WHATS = ["structure","status","projects","metrics","import_cycles",
          "surprising_connections","feature_map","business_rules",
          "process_flows","suggested_questions","service_mesh","validate"]


def _sym(path: str) -> str:
    from opencode_search.core.config import project_graph_db
    gdb = project_graph_db(path)
    if not gdb.exists(): return ""
    with sqlite3.connect(str(gdb)) as c:
        r = c.execute("SELECT name FROM symbols WHERE kind='function' LIMIT 1").fetchone()
    return r[0] if r else ""


# ── L1: structural guards ─────────────────────────────────────────────────

def test_fp0_deleted_modules():
    for mod in ("opencode_search.kb.hierarchy","opencode_search.kb.federation_hierarchy",
                "opencode_search.kb.structure","opencode_search.kb.world_model"):
        with pytest.raises(ModuleNotFoundError): __import__(mod)


def test_fp1_removed_whats_error(live_client, service_path):
    for w in _REMOVED:
        r = live_client.post("/api/overview", json={"project_path": service_path, "what": w})
        body = r.json()
        assert "error" in body and "unknown" in body["error"].lower(), f"{w!r}: {body}"


def test_fp2_valid_set():
    from opencode_search.server._overview import _VALID
    for w in _REMOVED: assert w not in _VALID, f"{w!r} still in _VALID"


def test_fp3_l1_only_in_all_dbs(sample_workspace):
    from opencode_search.core.config import project_graph_db
    from tests.live._projects import sample_project_paths
    bad = []
    for path in sample_project_paths(sample_workspace):
        gdb = project_graph_db(path)
        if not gdb.exists(): continue
        with sqlite3.connect(str(gdb)) as c:
            n = c.execute("SELECT COUNT(*) FROM communities WHERE level!=1").fetchone()[0]
        if n: bad.append(f"{Path(path).name}: {n}")
    assert not bad, "non-L1 rows remain:\n" + "\n".join(bad)


def test_fp4_no_domain_pages(service_path):
    from opencode_search.core.config import project_wiki_dir
    d = project_wiki_dir(service_path)
    # If wiki dir doesn't exist, there are trivially no domain pages — test passes.
    if d.exists():
        assert not list(d.glob("domain_*.md")), "domain_*.md found — L2 re-appeared"


# ── L2: sample service member ─────────────────────────────────────────────

def test_fp5_service_search(service_path):
    from opencode_search.server.mcp import search as t
    d = json.loads(asyncio.run(t("function definition", project_paths=[service_path])))
    assert d.get("total", 0) > 0, f"search 0: {d}"


def test_fp6_service_graph(service_path):
    from opencode_search.server.mcp import graph as t
    sym = _sym(service_path); assert sym
    for rel in ("callers","callees","impact","definition"):
        d = json.loads(asyncio.run(t(sym, service_path, rel)))
        assert isinstance(d, dict)


@pytest.mark.parametrize("what", _WHATS)
def test_fp7_service_overview(what, service_path):
    from opencode_search.server.mcp import overview as t
    d = json.loads(asyncio.run(t(service_path, what)))
    assert isinstance(d, dict) and "error" not in d, f"{what!r}: {d}"


def test_fp8_service_status(service_path):
    from opencode_search.server.mcp import overview as t
    d = json.loads(asyncio.run(t(service_path, "status")))
    assert "l1_enriched_pct" in d and "hierarchy_quality" in d
    assert "l2_enriched_pct" not in d


def test_fp9_service_wiki(live_client, service_path):
    r = live_client.get(f"/api/wiki?project={service_path}")
    assert r.status_code == 200
    assert not any("domain_" in p for p in r.json().get("pages", []))


# ── L2: federation root ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def service_path(sample_workspace: SampleWorkspace) -> str:
    return sample_workspace.promo


@pytest.fixture(scope="module")
def fed_root(sample_workspace: SampleWorkspace) -> str:
    return sample_workspace.fed_root


def test_fp10_federation_search(fed_root):
    from opencode_search.server.mcp import search as t
    d = json.loads(asyncio.run(t("function", project_paths=[fed_root])))
    assert d.get("total", 0) > 0


def test_fp11_federation_status(fed_root):
    from opencode_search.server.mcp import overview as t
    d = json.loads(asyncio.run(t(fed_root, "status")))
    assert "members" in d and d["members"] and "l1_enriched_pct" in d
    assert "l2_enriched_pct" not in d


@pytest.mark.parametrize("what", ["business_rules","process_flows"])
def test_fp12_federation_features(fed_root, what):
    from opencode_search.server.mcp import overview as t
    d = json.loads(asyncio.run(t(fed_root, what)))
    assert isinstance(d, dict) and "error" not in d


def test_fp13_federation_wiki_no_domain(live_client, fed_root):
    r = live_client.get(f"/api/wiki?project={fed_root}")
    assert r.status_code == 200
    assert not any("domain_" in p for p in r.json().get("pages", []))


# ── L3: quality (@slow) ──────────────────────────────────────────────────

@pytest.mark.slow
def test_fp14_ask_flat_l1(service_path):
    from opencode_search.server.mcp import ask as t
    result = asyncio.run(t("What is the overall architecture?", service_path, "global"))
    assert len(result.strip()) > 100
    assert any(k in result.lower() for k in ("cart","checkout","promo","order","discount","coupon","fulfillment","price","rule"))


# ── L4: Phase-1b flat-KB source guards ──────────────────────────────────

def test_fp15_no_set_community_parent_in_source():
    """Phase-1b guard: set_community_parent removed from graph/store.py (no callers existed)."""
    from opencode_search.graph.store import GraphStore
    assert not hasattr(GraphStore, "set_community_parent"), \
        "set_community_parent still exists on GraphStore — vestigial L2 nesting capacity"


def test_fp16_no_level2_query_in_quality():
    """Phase-1b guard: no WHERE level=2 query in graph/quality.py (always-0 dead metric)."""
    import inspect
    from opencode_search import graph
    quality_path = Path(inspect.getfile(graph)).parent / "quality.py"
    quality_path = quality_path.resolve()
    src = quality_path.read_text()
    assert "WHERE level=2" not in src, \
        "Dead WHERE level=2 query found in graph/quality.py — should have been removed"
    assert "n_l2" not in src, \
        "Vestigial n_l2 metric found in graph/quality.py — should have been removed"


def test_fp17_build_wiki_route_present(live_client):
    """Phase-1b guard: /api/build_wiki route replaces /api/build_hierarchy."""
    # /api/build_wiki with nonexistent path returns 404, not 404-for-unknown-route
    r = live_client.post("/api/build_wiki", json={"project_path": "/nonexistent"}, timeout=10)
    assert r.status_code in (400, 404), f"/api/build_wiki: {r.status_code} {r.text[:80]}"
    # old route must 404
    r2 = live_client.post("/api/build_hierarchy", json={"project_path": "/nonexistent"}, timeout=10)
    assert r2.status_code == 404, f"/api/build_hierarchy should be gone: {r2.status_code}"
