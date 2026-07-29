"""WS-F/WS-G: prove surviving features work after WS-B hierarchy deletion + WS-E purge.

R0 folded the tier-3 deletion into the same two lists this file was already built around.
`_REMOVED` and `_WHATS` are complements — one names what `overview` must reject, the other
what it must answer — so the five tier-3 variants move from the second list to the first
rather than simply leaving. That keeps fp1/fp2 discriminating: a variant that came back
would be caught by _REMOVED, and one that was dropped by mistake by _WHATS.
"""
from __future__ import annotations
import asyncio, json, sqlite3
from pathlib import Path
import pytest

from tests.live._sample_workspace import SampleWorkspace

pytestmark = pytest.mark.live

_RSE_SRC = Path(__file__).resolve().parents[3]  # source-file reads only; NOT passed to daemon
_REMOVED = ["hierarchy", "architecture_domains", "world_model",
            "patterns", "process_flows", "service_mesh", "business_rules", "feature_map",
            "suggested_questions"]
_WHATS = ["structure","status","projects","metrics","import_cycles",
          "surprising_connections","communities","validate"]


def _sym(path: str) -> str:
    from rag_search.core.config import project_graph_db
    gdb = project_graph_db(path)
    if not gdb.exists(): return ""
    with sqlite3.connect(str(gdb)) as c:
        r = c.execute("SELECT name FROM symbols WHERE kind='function' LIMIT 1").fetchone()
    return r[0] if r else ""


# ── L1: structural guards ─────────────────────────────────────────────────

def test_fp0_deleted_modules():
    # WS-B's four, plus the tier-3 modules R0c deleted. Same assertion, wider subject: the
    # generative KB left as a unit, so the import guard that proved WS-B landed is also the
    # cheapest proof that R0c did — a re-added kb/bpre.py fails here before anything imports it.
    for mod in ("rag_search.kb.hierarchy","rag_search.kb.federation_hierarchy",
                "rag_search.kb.structure","rag_search.kb.world_model",
                "rag_search.kb.bpre","rag_search.kb.bpre_ast","rag_search.kb.okf",
                "rag_search.kb.patterns","rag_search.kb.resolve_rerank",
                "rag_search.kb.valueflow","rag_search.kb.wiki",
                "rag_search.graph.enrich","rag_search.graph.llm"):
        with pytest.raises(ModuleNotFoundError): __import__(mod)


def test_fp1_removed_whats_error(live_client, service_path):
    for w in _REMOVED:
        r = live_client.post("/api/overview", json={"project_path": service_path, "what": w})
        body = r.json()
        assert "error" in body and "unknown" in body["error"].lower(), f"{w!r}: {body}"


def test_fp2_valid_set():
    """_REMOVED is absent from _VALID, and _WHATS is exactly _VALID.

    The equality is the half that was missing. This module's docstring has always claimed the two
    lists are complements, but only the _REMOVED direction was checked — so `communities` sat
    outside both for as long as it has existed, answered by the tool and exercised by no fp7 case.
    Deriving the positive side from _VALID means a new `what` cannot be added without a row here
    ([[feedback_allowlist_needs_sufficiency_test]]).
    """
    from rag_search.server._overview import _VALID
    for w in _REMOVED: assert w not in _VALID, f"{w!r} still in _VALID"
    assert set(_WHATS) == _VALID, (
        f"_WHATS must equal _VALID; missing={_VALID - set(_WHATS)} extra={set(_WHATS) - _VALID}"
    )


def test_fp3_l1_only_in_all_dbs(sample_workspace):
    from rag_search.core.config import project_graph_db
    from tests.live._projects import sample_project_paths
    bad = []
    for path in sample_project_paths(sample_workspace):
        gdb = project_graph_db(path)
        if not gdb.exists(): continue
        with sqlite3.connect(str(gdb)) as c:
            n = c.execute("SELECT COUNT(*) FROM communities WHERE level!=1").fetchone()[0]
        if n: bad.append(f"{Path(path).name}: {n}")
    assert not bad, "non-L1 rows remain:\n" + "\n".join(bad)


# fp4, fp9 and fp13 were three views of one property — no `domain_*.md` page anywhere, the
# WS-B guard that L2 nesting had not come back — read off the wiki directory, the per-project
# `/api/wiki` listing and the federation-root one. All three died with the wiki: `kb/wiki.py`
# and `core.config.project_wiki_dir` are gone, and there is no route left to list pages from.
# The property does not need re-pointing either, because fp3 asserts it at the source that
# actually mattered: `communities WHERE level!=1` must be empty in every sample graph.db, and
# a page could only ever have been rendered from such a row.


# ── L2: sample service member ─────────────────────────────────────────────

def test_fp5_service_search(service_path):
    from rag_search.server.mcp import search as t
    d = json.loads(asyncio.run(t("function definition", project_paths=[service_path])))
    assert d.get("total", 0) > 0, f"search 0: {d}"


def test_fp6_service_graph(service_path):
    from rag_search.server.mcp import graph as t
    sym = _sym(service_path); assert sym
    for rel in ("callers","callees","impact","definition"):
        d = json.loads(asyncio.run(t(sym, service_path, rel)))
        assert isinstance(d, dict)


@pytest.mark.parametrize("what", _WHATS)
def test_fp7_service_overview(what, service_path):
    from rag_search.server.mcp import overview as t
    d = json.loads(asyncio.run(t(service_path, what)))
    assert isinstance(d, dict) and "error" not in d, f"{what!r}: {d}"


def test_fp8_service_status(service_path):
    # `l1_enriched_pct`/`l2_enriched_pct` were the level-1-vs-level-2 summary fill rates, and
    # both left with the summariser: R0b removed the `enriching` state from the status ladder
    # because structural labelling fills every summary deterministically, which would have
    # pinned the percentage at a permanent 100. `index_state` is what discriminates now, and
    # `hierarchy_quality` (Leiden partition quality — no LLM in it) is unchanged.
    from rag_search.server.mcp import overview as t
    d = json.loads(asyncio.run(t(service_path, "status")))
    assert "index_state" in d and "hierarchy_quality" in d
    assert d["index_state"] in ("indexing", "degraded", "ready"), d["index_state"]
    assert "l1_enriched_pct" not in d and "l2_enriched_pct" not in d


# ── L2: federation root ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def service_path(sample_workspace: SampleWorkspace) -> str:
    return sample_workspace.promo


@pytest.fixture(scope="module")
def fed_root(sample_workspace: SampleWorkspace) -> str:
    return sample_workspace.fed_root


def test_fp10_federation_search(fed_root):
    from rag_search.server.mcp import search as t
    d = json.loads(asyncio.run(t("function", project_paths=[fed_root])))
    assert d.get("total", 0) > 0


def test_fp11_federation_status(fed_root):
    # Same re-pointing as fp8; the load-bearing half here is `members`, which proves federation
    # expansion still resolves after R0 — the one failure mode that would have made the deletion
    # unsafe, since BPRE's `federation_discover` and `daemon/federation.py` share a name and
    # nothing else.
    from rag_search.server.mcp import overview as t
    d = json.loads(asyncio.run(t(fed_root, "status")))
    assert "members" in d and d["members"] and "index_state" in d
    assert all("index_state" in m for m in d["members"]), d["members"][:2]
    assert "l1_enriched_pct" not in d and "l2_enriched_pct" not in d


# fp12 asked the federation root for `business_rules` and `process_flows` — two of the five
# tier-3 overview variants R0a dropped from `_VALID`. It is not deleted so much as inverted:
# both names are in `_REMOVED` now, so fp1 asserts the daemon *rejects* them and fp2 that
# they are absent from `_VALID`, which is a stronger statement than the one made here.


# ── L3: quality (@slow) ──────────────────────────────────────────────────

@pytest.mark.slow
def test_fp14_ask_flat_l1(service_path):
    from rag_search.server.mcp import ask as t
    result = asyncio.run(t("What is the overall architecture?", service_path, "global"))
    assert len(result.strip()) > 100
    assert any(k in result.lower() for k in ("cart","checkout","promo","order","discount","coupon","fulfillment","price","rule"))


# ── L4: Phase-1b flat-KB source guards ──────────────────────────────────

def test_fp15_no_set_community_parent_in_source():
    """Phase-1b guard: set_community_parent removed from graph/store.py (no callers existed)."""
    from rag_search.graph.store import GraphStore
    assert not hasattr(GraphStore, "set_community_parent"), \
        "set_community_parent still exists on GraphStore — vestigial L2 nesting capacity"


def test_fp16_no_level2_query_in_quality():
    """Phase-1b guard: no WHERE level=2 query in graph/quality.py (always-0 dead metric)."""
    import inspect
    from rag_search import graph
    quality_path = Path(inspect.getfile(graph)).parent / "quality.py"
    quality_path = quality_path.resolve()
    src = quality_path.read_text()
    assert "WHERE level=2" not in src, \
        "Dead WHERE level=2 query found in graph/quality.py — should have been removed"
    assert "n_l2" not in src, \
        "Vestigial n_l2 metric found in graph/quality.py — should have been removed"


def test_fp17_no_llm_in_graph_handler():
    """Gap-B guard: impact_narrative + semantic_trace deleted (P2 violation — LLM in query path)."""
    import importlib
    mod = importlib.import_module("rag_search.query.graph_handler")
    assert not hasattr(mod, "impact_narrative"), \
        "impact_narrative re-introduced in query/graph_handler (P2 violation: LLM in query path)"
    assert not hasattr(mod, "semantic_trace"), \
        "semantic_trace re-introduced in query/graph_handler (P2 violation: LLM in query path)"


# fp18 asserted /api/build_wiki had *replaced* /api/build_hierarchy — a Phase-1b succession
# guard whose winning side R0a then deleted. It could not simply be inverted here: its
# `in (400, 404)` acceptance means it stayed green against a route that no longer exists,
# which is the shape of a guard that has quietly stopped discriminating. The surviving
# statement lives in test_p5_server.py::test_e7_trimmed_http_surface, whose `deleted` list
# now names /api/build_wiki, /api/wiki and /api/kb_health and demands a hard 404/405.
