"""WS-F/WS-G: prove surviving features work after WS-B hierarchy deletion + WS-E purge."""
from __future__ import annotations
import asyncio, json, sqlite3
from pathlib import Path
import pytest
pytestmark = pytest.mark.live

_OSE = str(Path(__file__).resolve().parents[3])
_REMOVED = ["hierarchy", "architecture_domains", "world_model"]
_WHATS = ["structure","status","projects","metrics","import_cycles",
          "surprising_connections","feature_map","business_rules",
          "process_flows","suggested_questions","service_mesh","validate"]


def _astro() -> str:
    from opencode_search.core.registry import list_projects
    from opencode_search.daemon.federation import expand_federation
    return next((e.path for e in list_projects()
                 if e.enabled and len(expand_federation(e.path)) > 1), "")


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


def test_fp1_removed_whats_error(live_client):
    for w in _REMOVED:
        r = live_client.post("/api/overview", json={"project_path": _OSE, "what": w})
        body = r.json()
        assert "error" in body and "unknown" in body["error"].lower(), f"{w!r}: {body}"


def test_fp2_valid_set():
    from opencode_search.server._overview import _VALID
    for w in _REMOVED: assert w not in _VALID, f"{w!r} still in _VALID"


def test_fp3_l1_only_in_all_dbs():
    from opencode_search.core.config import project_graph_db
    from opencode_search.core.registry import list_projects
    bad = []
    for e in list_projects():
        gdb = project_graph_db(e.path)
        if not gdb.exists(): continue
        with sqlite3.connect(str(gdb)) as c:
            n = c.execute("SELECT COUNT(*) FROM communities WHERE level!=1").fetchone()[0]
        if n: bad.append(f"{Path(e.path).name}: {n}")
    assert not bad, "non-L1 rows remain:\n" + "\n".join(bad)


def test_fp4_no_domain_pages():
    from opencode_search.core.config import project_wiki_dir
    d = project_wiki_dir(_OSE)
    # If wiki dir doesn't exist, there are trivially no domain pages — test passes.
    if d.exists():
        assert not list(d.glob("domain_*.md")), "domain_*.md found — L2 re-appeared"


# ── L2: OSE standalone ───────────────────────────────────────────────────

def test_fp5_ose_search():
    from opencode_search.server.mcp import search as t
    d = json.loads(asyncio.run(t("function definition", project_paths=[_OSE])))
    assert d.get("total", 0) > 0, f"search 0: {d}"


def test_fp6_ose_graph():
    from opencode_search.server.mcp import graph as t
    sym = _sym(_OSE); assert sym
    for rel in ("callers","callees","impact","definition"):
        d = json.loads(asyncio.run(t(sym, _OSE, rel)))
        assert isinstance(d, dict)


@pytest.mark.parametrize("what", _WHATS)
def test_fp7_ose_overview(what):
    from opencode_search.server.mcp import overview as t
    d = json.loads(asyncio.run(t(_OSE, what)))
    assert isinstance(d, dict) and "error" not in d, f"{what!r}: {d}"


def test_fp8_ose_status():
    from opencode_search.server.mcp import overview as t
    d = json.loads(asyncio.run(t(_OSE, "status")))
    assert "l1_enriched_pct" in d and "hierarchy_quality" in d
    assert "l2_enriched_pct" not in d


def test_fp9_ose_wiki(live_client):
    r = live_client.get(f"/api/wiki?project={_OSE}")
    assert r.status_code == 200
    assert not any("domain_" in p for p in r.json().get("pages", []))


# ── L2: astro-project (federation root) ─────────────────────────────────

@pytest.fixture(scope="module")
def astro_root():
    r = _astro()
    if not r: pytest.fail("no federated root registered — register astro-project first")
    return r


def test_fp10_astro_search(astro_root):
    from opencode_search.server.mcp import search as t
    d = json.loads(asyncio.run(t("function", project_paths=[astro_root])))
    assert d.get("total", 0) > 0


def test_fp11_astro_status(astro_root):
    from opencode_search.server.mcp import overview as t
    d = json.loads(asyncio.run(t(astro_root, "status")))
    assert "members" in d and d["members"] and "l1_enriched_pct" in d
    assert "l2_enriched_pct" not in d


@pytest.mark.parametrize("what", ["business_rules","process_flows"])
def test_fp12_astro_features(astro_root, what):
    from opencode_search.server.mcp import overview as t
    d = json.loads(asyncio.run(t(astro_root, what)))
    assert isinstance(d, dict) and "error" not in d


def test_fp13_astro_wiki_no_domain(live_client, astro_root):
    r = live_client.get(f"/api/wiki?project={astro_root}")
    assert r.status_code == 200
    assert not any("domain_" in p for p in r.json().get("pages", []))


# ── L3: quality (@slow) ──────────────────────────────────────────────────

@pytest.mark.slow
def test_fp14_ask_flat_l1():
    from opencode_search.server.mcp import ask as t
    result = asyncio.run(t("What is the overall architecture?", _OSE, "global"))
    assert len(result.strip()) > 100
    assert any(k in result.lower() for k in ("search","embed","graph","daemon","community","kb","query"))
