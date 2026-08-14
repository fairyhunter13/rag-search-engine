"""P5 server tests: MCP tools, HTTP routes, dashboard (no mocks)."""
import asyncio
import json
import re
import time
from pathlib import Path

import pytest

from tests.live._sample_workspace import SampleWorkspace
from tests.live._sweeps import local_sweeps_paused, sweeps_state

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def service_path(sample_workspace: SampleWorkspace) -> str:
    """Sample promo-svc — indexed, 7 L1 communities, has graph.db + vectors.db."""
    return sample_workspace.promo


def test_mcp_has_four_tools():
    """HR30: the MCP surface is exactly these 4 tools — `ask` was retired (see server/mcp.py).

    Asserted with `==`, not the `<=` this used. HR30 is stated as "MCP surface = 5 tools only" and
    a subset check cannot see a sixth tool, so the "only" half was never enforced — which is also
    why nothing failed when the surface was meant to be closed. Equality is the claim.
    """
    from rag_search.server.mcp import mcp
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert names == {"search", "graph", "overview", "index"}, f"unexpected MCP surface: {names}"


def test_mcp_graph_nonexistent_returns_error():
    """graph tool returns {error:...} JSON for an unindexed project."""
    from rag_search.server.mcp import graph as graph_tool
    result = asyncio.run(graph_tool("authenticate", "/nonexistent/path", "definition"))
    data = json.loads(result)
    assert "error" in data


def test_mcp_overview_projects_returns_list():
    """P15.4: overview(what='projects') returns ≥1 real registered project."""
    from rag_search.server.mcp import overview as overview_tool
    result = asyncio.run(overview_tool("", "projects"))
    data = json.loads(result)
    assert "projects" in data
    assert len(data["projects"]) >= 1, "daemon should have ≥1 registered project"


def test_mcp_overview_metrics():
    """P20.3: overview(what='metrics') returns chat_stream metrics dict."""
    from rag_search.server.mcp import overview as overview_tool
    result = asyncio.run(overview_tool("", "metrics"))
    data = json.loads(result)
    assert "chat_stream" in data, f"metrics missing chat_stream key: {result}"
    assert "stream_error_count" in data["chat_stream"], f"chat_stream missing stream_error_count: {data}"


def test_mcp_index_register_remove(safe_tmp_path):
    """index tool registers then removes a project without crashing."""
    from rag_search.server.mcp import index as index_tool
    p = str(safe_tmp_path)
    reg = json.loads(asyncio.run(index_tool(p, enabled=True)))
    assert reg["status"] in ("flagged", "already_registered")
    rem = json.loads(asyncio.run(index_tool(p, enabled=False)))
    assert rem["status"] in ("removed", "not_found")


def test_healthz(live_client):
    """P15.2: /healthz on the REAL daemon (production create_app)."""
    r = live_client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_healthz_reports_how_long_sweeps_have_been_paused(live_client):
    """`sweeps_paused_s` distinguishes sweeping from paused — the 4 h outage nothing could see.

    On 2026-07-30 eleven pause calls from separate live sessions, each correctly restoring the
    "already paused" state it found, left the daemon paused from ~12:03 to ~16:21. Every reconcile
    tick logged "abandoned before start (sweeps paused)" and nothing else disagreed: /healthz was
    green, searches answered from the existing index, and a purge of 373,387 chunks was wrongly
    suspected first. `_PAUSED` has no refcount and no lease, so a leaked pause is permanent.

    Both arms are asserted because only the pair discriminates: a field hardcoded to 0.0 passes the
    paused arm's absence, and one that merely counts uptime passes the sweeping arm's presence.
    The `sweeps_state(False)` window is one GET wide on purpose — it really does let the daemon
    sweep and contend for the GPU (HR41), so it must not hold a sleep.

    `sweeps_pause_lease_s` is asserted beside it because the two answer different questions and a
    poller needs both: the elapsed field says someone forgot, the lease field says whether it is
    about to heal itself. A published field nothing asserts is one a refactor can drop in silence.
    """
    with sweeps_state(False):
        sweeping = live_client.get("/healthz").json()
    with sweeps_state(True):
        time.sleep(1.2)
        paused = live_client.get("/healthz").json()
    assert sweeping["sweeps_paused_s"] == 0.0, (
        f"sweeps resumed but /healthz reports {sweeping['sweeps_paused_s']}s paused")
    assert sweeping["sweeps_pause_lease_s"] == 0.0, (
        "a resumed daemon still reports a live pause lease — the deadline outlived the pause")
    assert paused["sweeps_paused_s"] >= 1.0, (
        f"sweeps held paused for 1.2s but /healthz reports {paused['sweeps_paused_s']}s — a pause "
        "that reads as zero is exactly the blind spot this field exists to close"
    )
    assert 0.0 < paused["sweeps_pause_lease_s"] <= 1800.0, (
        f"a paused daemon reports {paused['sweeps_pause_lease_s']}s of lease left; an unbounded or "
        "absent lease is the 4 h outage, and this is the only field that would show it"
    )


def test_dashboard_views_present(live_client):
    """P15.2: /dashboard on the REAL daemon — every nav view is wired.

    `wiki` left with tier 3 and `docs` took its place. The assertion moved from a bare substring
    to the `vbtn-` nav id because a bare `"docs" in body` matches the word anywhere in the page
    (comments included) and would pass with the view deleted — the shape
    [[feedback_guard_tests_must_discriminate]] warns about, and the reason the old `"wiki" in body`
    kept passing while the view was being dismantled.
    """
    r = live_client.get("/dashboard")
    assert r.status_code == 200
    body = r.text.lower()
    for view in ("pulse", "chat", "admin", "graph"):
        assert f'id="vbtn-{view}"' in body, f"dashboard missing '{view}' nav button"
    assert 'id="vbtn-wiki"' not in body, "wiki nav button must not survive tier 3's deletion"
    assert 'id="vbtn-processes"' not in body, "processes nav button must not survive tier 3"


def test_api_projects_returns_list(live_client):
    """P15.2/P15.4: /api/projects returns ≥1 real registered project."""
    r = live_client.get("/api/projects")
    assert r.status_code == 200
    data = r.json()
    assert "projects" in data
    assert len(data["projects"]) >= 1, "live daemon should have ≥1 registered project"


def test_api_overview_projects(live_client):
    """P15.2/P15.4: /api/overview?what=projects returns ≥1 real project."""
    r = live_client.post("/api/overview", json={"what": "projects"})
    assert r.status_code == 200
    data = r.json()
    assert "projects" in data
    assert len(data["projects"]) >= 1, "live daemon should have ≥1 registered project"


def test_live_daemon_has_mcp_route(live_client):
    """P15.2 parity: production create_app() mounts /mcp (FastMCP streamable-HTTP).
    The test-only in-process app lacks this route; driving the live daemon proves
    tests exercise the real served surface, not a stripped-down variant.
    """
    # POST without MCP headers → 406 Not Acceptable (not 404 Not Found)
    r = live_client.post("/mcp", json={})
    assert r.status_code != 404, (
        f"/mcp not found — create_app() must mount FastMCP at /mcp; got {r.status_code}"
    )



# P9.2 asserted that detect_patterns() named ≥1 framework via the LLM rather than a static dict.
# It was the runtime half of the same pair as test_p14_mcp_readonly's A3 source-guard, and both
# died with kb/patterns.py — the module behind overview(what="patterns"), the one synchronous
# DeepSeek round trip that ever sat on a query path.


def test_index_tool_rejects_forbidden_root(safe_tmp_path):
    """P24.3: index(/tmp/...) must return status='forbidden' and NOT register the path."""
    from rag_search.core.registry import get_project
    from rag_search.server.mcp import index as index_tool

    bad = "/tmp/rse-test-forbidden-registration-check"
    result = json.loads(asyncio.run(index_tool(bad, enabled=True)))
    assert result["status"] == "forbidden", f"expected forbidden, got {result}"
    assert get_project(bad) is None, "forbidden path must NOT be registered"

    normal = str(safe_tmp_path)
    ok = json.loads(asyncio.run(index_tool(normal, enabled=True)))
    assert ok["status"] in ("flagged", "already_registered"), f"normal path failed: {ok}"
    asyncio.run(index_tool(normal, enabled=False))  # cleanup


def test_index_tool_e2e(safe_tmp_path):
    """P10.4b: enabled=True creates registry entry; enabled=False removes it + index dir.

    Every `index()` here spawns `reconcile_projects` on an unjoined daemon thread. In production
    that is harmless and intended: the MCP app is served by the daemon (`server/routes.py:91`), so
    the thread runs *in* the daemon, reads the daemon's `_PAUSED`, and honours the pause lease.

    Under pytest it is neither. The tool is imported and called in **this** process, where
    `sweeps._PAUSED` is a different module global that the suite's HTTP pause never touches — so
    the thread walks all ~210 registered projects at full speed, indexing and re-deriving, for the
    rest of the session. It is structurally exempt from the one mechanism that exists to stop
    exactly this, and it outlives the test that started it.

    That has now cost two CI runs on two unrelated tests, each passing in isolation:
    `test_pipeline_all_stages_rse_repo` read a graph store mid-re-derive, and
    `test_e1_rerank_reorders_search_results` searched a sample project while the same walk was
    re-indexing it. Neither names the cause; both look like flakes.

    So: hold the in-process pause across the calls, and **join** the threads instead of trusting
    them to lose a race. With the local pause set, `reconcile_projects` returns at its `is_paused()`
    guard and the join is immediate — the fixed cost of not leaving a fleet-wide sweep running
    inside a test process. See `docs/decisions/2026-07-31-atomic-graph-rederive.md`.
    """
    import threading

    from rag_search.core.config import index_dir
    from rag_search.core.registry import list_projects
    from rag_search.server.mcp import index as index_tool

    def _index(path: str, *, enabled: bool) -> dict:
        before = set(threading.enumerate())
        with local_sweeps_paused(True):
            out = json.loads(asyncio.run(index_tool(path, enabled=enabled)))
            for t in set(threading.enumerate()) - before:
                t.join(timeout=30)
                assert not t.is_alive(), (
                    f"index(enabled={enabled}) left {t.name!r} running past the test; a sweep that "
                    f"outlives the pause it was started under corrupts whatever runs next")
        return out

    p = str(safe_tmp_path)
    reg = _index(p, enabled=True)
    assert reg["status"] in ("flagged", "already_registered")
    assert any(proj.path == p for proj in list_projects()), "Project not in registry after register"
    # Re-registering is still polled: `_index` joins the reconcile thread, but the registry has a
    # second writer (the daemon), so "already_registered" remains eventually consistent.
    import time
    reg2 = {}
    for _ in range(50):
        reg2 = _index(p, enabled=True)
        if reg2["status"] == "already_registered":
            break
        time.sleep(0.1)
    assert reg2["status"] == "already_registered"
    idx = index_dir(p)
    idx.mkdir(parents=True, exist_ok=True)
    rem = _index(p, enabled=False)
    assert rem["status"] in ("removed", "not_found")
    assert not any(proj.path == p for proj in list_projects()), "Project still in registry after remove"
    assert not Path(idx).exists(), "Index dir not deleted after remove"


def test_overview_all_whats_real_federation_root(sample_workspace):
    """P10.4: every what= value returns parseable non-empty data on the real federation root."""
    from rag_search.server.mcp import overview as overview_tool
    from tests.live._projects import federation_root

    fed_root = federation_root()
    # Read off _overview._VALID minus the three that take no graph: projects, metrics, validate.
    # service_mesh / feature_map / business_rules / process_flows were the tier-3 half of this
    # list; R0a deleted them from _VALID, so leaving them here would fail on the "unknown what="
    # branch — the list has to shrink with the tool, not outlive it.
    # `suggested_questions` was the seventh and left with the chat box it seeded.
    whats = [
        "structure", "communities", "status", "import_cycles",
        "surprising_connections",
    ]
    for what in whats:
        result = asyncio.run(overview_tool(fed_root, what))
        data = json.loads(result)
        assert data, f"overview(what={what!r}) returned empty dict: {result[:120]}"


# The service_mesh non-emptiness test went with `_detect_services`, which delegated to
# kb/bpre_ast.py::federation_discover. Nothing re-points: federation *membership* is still
# asserted (daemon/federation.py::expand_federation, exercised above and in DK1), but the
# gRPC/HTTP service-surface view it produced does not exist any more.


def test_community_read_orders_by_member_count_no_operationalerror(service_path):
    """P23.2: ORDER BY member_count (not node_count) — no OperationalError on real graph DBs.

    The regression this guards is a column that does not exist in the communities table, and it
    was originally caught through /api/suggested_questions. That route is gone; the same SQL is
    now the only community read on a served surface, in _overview's `communities` branch, which
    orders by member_count for the cap and again after the federation concatenation. Asserting the
    rows come back is what witnesses the column name — an OperationalError would surface as an
    `error` key rather than an exception, so the payload has to be inspected, not just the call.
    """
    from rag_search.server.mcp import overview as overview_tool
    data = json.loads(asyncio.run(overview_tool(service_path, "communities")))
    assert "error" not in data, f"communities read failed (likely node_count bug): {data}"
    assert "communities" in data, f"communities payload missing its rows: {data}"


def test_mcp_search_subdir_resolves_to_root(service_path):
    """P23.1: search with a non-root project_paths resolves to the enclosing registered root."""

    from rag_search.server.mcp import search as search_tool

    subdir = str(Path(service_path) / "src")
    result = json.loads(asyncio.run(search_tool("function definition", project_paths=[subdir])))
    assert result["projects_searched"] == [service_path], (
        f"subdir {subdir!r} must resolve to root {service_path!r}; got {result['projects_searched']}"
    )
    assert result["total"] > 0, "Expected results from indexed project"

    # A scope that resolved to no store at all is an error naming what went unread, not an empty
    # result set (cc3d208 / SU1). This arm used to assert `total == 0`, which is a statement about
    # the *query* standing in for one about the *index* — the two are indistinguishable at the call
    # site and only one of them means "look somewhere else".
    outside = json.loads(asyncio.run(search_tool("function definition", project_paths=["/nonexistent/path"])))
    assert "error" in outside, f"a search that opened zero stores reported an ordinary miss: {outside}"
    assert "/nonexistent/path" in outside.get("unindexed", []), (
        f"the error must name the scope it could not read, or it cannot be acted on: {outside}")
    assert not outside.get("results"), outside


def test_auto_pipeline_status_real(live_client, safe_tmp_path):
    """P19.6: /api/auto_pipeline_status returns real enabled/pending — not canned data.

    Register an un-indexed tmp project → it must appear in pending.
    Pause sweeps → enabled must flip to False.
    """
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import remove_project, upsert_project

    proj_path = str(safe_tmp_path)

    # enabled=True is unreachable while the autouse session fixture holds sweeps paused, so
    # resume for just this read. Sequential rather than nested with the block below: the
    # project must never be registered-but-unindexed while sweeps are running, or the daemon
    # races to index it. `sweeps_state` restores the session's pause either way.
    with sweeps_state(paused=False):
        r0 = live_client.get("/api/auto_pipeline_status")
        assert r0.status_code == 200, f"unexpected status {r0.status_code}"
        d0 = r0.json()
        assert "enabled" in d0 and "pending" in d0, f"missing keys: {d0}"
        assert d0["enabled"] is True, f"expected enabled=True after explicit resume, got {d0}"

    # Pause sweeps, THEN register — pending check is now race-free
    with sweeps_state(paused=True):
        try:
            upsert_project(ProjectEntry(path=proj_path, enabled=True))
            r = live_client.get("/api/auto_pipeline_status")
            d = r.json()
            assert d["enabled"] is False, f"expected enabled=False after pause, got {d}"
            assert proj_path in d["pending"], (
                f"un-indexed {proj_path} must appear in pending; got {d['pending'][:3]}"
            )
        finally:
            # Inside the pause: deregister before sweeps can resume, for the same reason.
            remove_project(proj_path)


# P25.1 is gone with tier 3. It asserted /api/kb_health's enriched_pct counted communities with a
# DeepSeek *summary* rather than merely a title. Structural labelling writes a templated summary for
# every community it labels, so the ratio it measured is now a permanent 100% by construction — a
# gate that can only ever read one value is not a gate. P27 below still covers the property that
# survived: the labelling pass keys on summary, not title.


def test_label_project_uses_summary_gate(safe_tmp_path):
    """P27: _label_project labels titled-but-unsummarized communities."""
    import sqlite3  # noqa: I001
    from rag_search.core.config import project_graph_db
    from rag_search.daemon.sweeps import _label_project
    from rag_search.graph.community import detect_communities
    from rag_search.graph.extractor import extract_symbols, symbol_id
    from rag_search.graph.store import GraphStore
    proj = str(safe_tmp_path)
    fpath = safe_tmp_path / "auth.py"
    fpath.write_text("def authenticate(token): pass\ndef validate(t): return bool(t)\n")
    gs = GraphStore(project_graph_db(proj))
    try:
        for sym in extract_symbols(fpath, fpath.read_text(), "python"):
            gs.upsert_symbol(symbol_id(str(fpath), sym.name, sym.start_line),
                             sym.name, sym.qualified_name, sym.kind,
                             str(fpath), sym.start_line, sym.end_line, sym.language)
        gs.commit()
        detect_communities(gs)
        assert gs.community_count() > 0
        titled = gs._con.execute(
            "SELECT COUNT(*) FROM communities WHERE title IS NOT NULL AND title != ''"
        ).fetchone()[0]
        assert titled > 0, "P21 must set structural labels before the summary pass"
    finally:
        gs.close()
    _label_project(proj)
    with sqlite3.connect(str(project_graph_db(proj))) as con:
        post = con.execute(
            "SELECT COUNT(*) FROM communities WHERE summary IS NOT NULL AND summary != ''"
        ).fetchone()[0]
    assert post > 0, "the labelling gate must use summary IS NULL, not title IS NULL"


# P28.1 and P28.2 are gone with tier 3: both POSTed /api/build_wiki, a route R0a deleted along with
# the wiki generator behind it.


def test_overview_status_has_index_state(service_path):
    """P25.2: overview(what='status') returns index_state in the 3-value ladder.

    The old ladder had a fourth rung, `enriching`, and a companion `enriched_pct`. Both keyed on
    DeepSeek narration, so they left with it; the surviving states are the ones a keyless box can
    actually reach.
    """
    from rag_search.server.mcp import overview as overview_tool

    data = json.loads(asyncio.run(overview_tool(service_path, "status")))
    assert "index_state" in data, f"index_state missing from status: {data}"
    assert data["index_state"] in ("indexing", "degraded", "ready"), (
        f"index_state={data['index_state']!r} not in expected set"
    )
    assert "enriched_pct" not in data, (
        f"enriched_pct must not survive tier 3's deletion: {data}"
    )


def test_overview_unknown_what_returns_error():
    """G4: overview(what='bogus') returns {error, valid} instead of silently falling through."""
    from rag_search.server.mcp import overview as overview_tool
    result = asyncio.run(overview_tool("", "bogus_unknown_what"))
    data = json.loads(result)
    assert "error" in data, f"expected error key, got: {data}"
    assert "valid" in data, f"expected valid key, got: {data}"
    assert "structure" in data["valid"], f"'structure' missing from valid list: {data['valid']}"


def test_graph_no_project_path_no_roots_fails_loud(sample_workspace):
    """G5 (updated 2026-07-14): graph(symbol) with no project_path and no client roots must FAIL
    LOUD with candidates — never silently resolve to the arbitrary first enabled project (that
    silent fallback mis-answered about unrelated projects). Correct
    cwd-aware auto-resolution now comes from MCP client roots — see test_roots_default_project.py.
    """
    from rag_search.core.registry import list_projects
    from rag_search.server.mcp import graph as graph_tool

    assert len([p for p in list_projects() if p.enabled]) > 1, (
        "test presumes multiple enabled projects (sample_workspace + live registry)"
    )
    data = json.loads(asyncio.run(graph_tool("own_fn")))  # ctx=None → no roots
    assert "project_path required" in data.get("error", ""), (
        f"G5: expected fail-loud on empty project_path, got: {data}"
    )
    assert isinstance(data.get("candidates"), list) and data["candidates"], data


def test_graph_and_overview_no_project_path_fail_loud(sample_workspace):
    """G5b (updated 2026-07-14): a tool called with an empty project_path and no client roots
    must fail loud (listing candidates), not silently answer about an arbitrary first-enabled
    project. This closes the transparency gap the old disclosure-prefix only papered over: a
    silent projects[0] pick mis-answered about unrelated projects. Correct
    cwd-aware auto-resolution now comes from MCP client roots — see test_roots_default_project.py.

    `ask` was the second witness until 2026-07-29. `graph` replaces it rather than the case being
    halved: the property belongs to `_default_or_error`, which both reach identically, and two
    witnesses are what proves the ladder is shared rather than reimplemented per tool.
    """
    from rag_search.core.registry import list_projects
    from rag_search.server.mcp import graph as graph_tool
    from rag_search.server.mcp import overview as overview_tool

    assert len([p for p in list_projects() if p.enabled]) > 1, (
        "test presumes multiple enabled projects (sample_workspace + live registry)"
    )
    overview_data = json.loads(asyncio.run(overview_tool("", "structure")))  # ctx=None → no roots
    assert "project_path required" in overview_data.get("error", ""), overview_data
    assert overview_data.get("candidates"), overview_data

    graph_result = asyncio.run(graph_tool("anything", ""))
    assert "project_path required" in graph_result, (
        f"graph() must fail loud on empty project_path, got: {graph_result[:200]!r}"
    )


_RSE_SRC = Path(__file__).resolve().parents[3]  # source-file reads only; NOT passed to daemon



def test_reranking_is_query_time_only():
    """T-R2: index/ and kb/ packages must not directly invoke the cross-encoder.

    The reranking IMPLEMENTATION (rerank_passages) lives only in query/search.py.

    This scan used to carry one exception, kb/resolve_rerank.py — BPRE's Tier-1.75 bridge and
    the single point where kb/ was allowed to reach the cross-encoder. It left with tier 3, and
    the exception had to leave with it rather than sit here unused: an exemption for a deleted
    file is a silent hole, since a future kb/resolve_rerank.py would inherit it. With the
    exception gone the rule is flat — nothing under index/ or kb/ may name rerank_passages —
    which is also what test_inference_lanes.py::test_rerank_passages_only_in_gpu_lane now
    enforces tree-wide from the allowlist side.
    """

    base = Path(__file__).parents[2] / "rag_search"
    for pkg in [base / "index", base / "kb"]:
        for py in pkg.rglob("*.py"):
            src = py.read_text()
            assert "rerank_passages" not in src, (
                f"Direct rerank_passages call found in {py.relative_to(base.parent)} — "
                "the cross-encoder is query-time only; keep it in query/search.py"
            )


def test_search_reranks_full_pool(mini_stores, embedder):
    """T-R3/C2: search() reranks the entire scope-filtered pool (no pre-truncation).

    mini_stores has ~6 chunks. With top_k=2: old code would rerank only top_k*2=4;
    new code reranks all top_k*3=6 (or fewer if scope-filtered).  Results must be
    non-empty, carry rerank_score, and be monotonic desc by that score.
    """
    from rag_search.index.store import VectorStore
    from rag_search.query.search import search

    vs = VectorStore(mini_stores["vdb"])
    try:
        results = search("authenticate user token", embedder, vs, scope="code", top_k=2)
    finally:
        vs.close()
    assert results, "expected at least 1 result from mini_stores"
    rscores = [r.get("rerank_score") for r in results]
    assert all(s is not None for s in rscores), f"some results missing rerank_score: {rscores}"
    assert all(rscores[i] >= rscores[i + 1] for i in range(len(rscores) - 1)), (
        f"results not ordered by rerank_score: {rscores}"
    )


def test_e1_rerank_reorders_search_results(service_path):
    """E1/HR8: MCP search on sample service — rerank_score sorted desc, lift detected on ≥1 of 4 queries.

    Read at verbosity="full" since 2026-07-29. The compact projection reports a single
    `score` = `rerank_score` *or* the vector score if reranking never happened, so both of
    this gate's assertions go blind on it: `rerank_score` is absent, and comparing "top by
    score" against "first result" is comparing a list to itself, which finds lift always and
    a dead reranker never. Full is the only shape that still carries the two scores apart.

    Lift is the *whole ranking*, compared by `chunk_id`, not the top-1 `path`. Top-1-by-path
    was flaky and weak at the same time, for one reason: most results share a file, so a
    rerank that reorders chunks *within* a file registered as no lift at all. That left the
    signal resting on the few cross-file flips, whose vector-score gaps were measured at
    0.0017 and ~0.0000 (a tie at four decimals) — and two builds of the same fixture, on
    identical code, produced 2/4 and 1/4 lifting queries. A third yielding 0/4 turns CI red
    with nothing regressed, which is what happened on 2026-07-31. Comparing full order gives
    4/4 queries with 5-8 of 8 positions displaced, on the same builds.

    It is also the stricter gate, not the looser one: a pass-through reranker returns vector
    order, and `sorted` is stable, so ties can never manufacture a difference. Any reordering
    at all now counts, including the within-file kind the old form could not see.
    """
    from rag_search.server.mcp import search as _mcp_search
    queries = [
        "discount rule application",
        "coupon validation logic",
        "order processing function",
        "checkout service integration",
    ]
    lift_found = False
    for q in queries:
        data = json.loads(
            asyncio.run(_mcp_search(q, project_paths=[service_path], verbosity="full"))
        )
        res = data.get("results", [])
        assert res, f"E1: no results for {q!r}"
        rs = [r.get("rerank_score") for r in res]
        assert all(s is not None for s in rs), f"E1: missing rerank_score: {rs}"
        assert all(rs[i] >= rs[i+1] for i in range(len(rs)-1)), f"E1: unsorted: {rs}"
        ids = [r.get("chunk_id") for r in res]
        assert all(i is not None for i in ids), f"E1: missing chunk_id: {ids}"
        if len(res) > 1:
            vec_order = [r.get("chunk_id") for r in
                         sorted(res, key=lambda r: r.get("score", 0.0), reverse=True)]
            if vec_order != ids:
                lift_found = True
    assert lift_found, "E1: rerank never reordered vs vector order (pass-through?)"
    src = (Path(__file__).parents[2] / "rag_search" / "server" / "mcp.py").read_text()
    assert 'sort(key=lambda r: r.get("score"' not in src, "E1 guard: bare score sort in mcp.py"


def test_e2_ask_context_is_rerank_ordered(service_path):
    """E2/HR8: assembled context carries path markers, first chunk = rerank top-1.

    Read through `run_ask` since 2026-07-29 rather than the retired MCP tool. That is where the
    property always lived — the tool was a one-line `asyncio.to_thread(run_ask, …)` — and it is
    now the dashboard chat's context builder, which is the consumer that cannot loop and so
    depends on this ordering being right the first time.
    """
    from rag_search.query.ask import run_ask
    from rag_search.server.mcp import search as _mcp_search
    q = "how does the promotion rule engine apply discounts"
    ctx = run_ask(q, service_path, "all")
    assert ctx and "[" in ctx, f"E2: empty or no path markers: {ctx[:80]}"
    top = json.loads(asyncio.run(_mcp_search(q, project_paths=[service_path])))["results"]
    assert top, "E2: search returned no results"
    assert top[0].get("path", "") in ctx, (
        f"E2: rerank top-1 {top[0].get('path')!r} not found in ask context"
    )
    # Structural guard: assembled context starts with a section header, not LLM prose.
    assert ctx.startswith("## "), "E2: LLM prose in ctx — assembled context must start with ## section marker"


def test_e3_community_context_is_reranked(service_path):
    """E3/HR8/D2: compose_answer(scope=architecture) top community differs between distinct queries."""
    from rag_search.core.config import project_graph_db
    from rag_search.graph.store import GraphStore
    from rag_search.query.ask import compose_answer
    gdb = project_graph_db(service_path)
    assert gdb.exists(), (
        "sample promo-svc graph DB not found — sample_workspace fixture must run first"
    )
    gs = GraphStore(gdb)
    try:
        a = compose_answer("how does coupon validation work", [], [gs], scope="architecture")
        b = compose_answer("how does checkout integration work", [], [gs], scope="architecture")
    finally:
        gs.close()
    assert a, "E3: compose_answer empty for query A"
    assert b, "E3: compose_answer empty for query B"
    # scope="architecture" always leads with the "## Architecture" section, so both assemblies
    # share their framing; anything that differs is the reranked community order itself.
    # (This scope carries the ordering `global` used to name — see ask.py::_SCOPES.)
    assert a != b, "E3: compose_answer identical for two distinct queries (reranking is static?)"
    ask_src = (Path(__file__).parents[2] / "rag_search" / "query" / "ask.py").read_text()
    assert "rerank_passages" in ask_src, "E3 guard: ask.py must use rerank_passages"
    assert "argsort" not in ask_src, "E3 guard: ask.py must not use argsort"


def test_e4_rerank_lift_metric(live_client, service_path):
    """E4/D3: /api/metrics exposes rerank block; in-process search increments the counter."""
    from rag_search.query.search import rerank_stats
    from rag_search.server.mcp import search as _mcp_search
    # Structure check via live daemon HTTP endpoint
    daemon_data = live_client.get("/api/metrics").json()
    assert "rerank" in daemon_data, f"E4: rerank block missing: {daemon_data}"
    assert isinstance(daemon_data["rerank"].get("queries"), int), "E4: rerank.queries not int"
    assert isinstance(daemon_data["rerank"].get("top1_changed"), int), "E4: top1_changed not int"
    # Counter increment check via in-process call (daemon has its own counter per-process)
    before = rerank_stats()["queries"]
    N = 3
    for i in range(N):
        asyncio.run(_mcp_search(f"discount rule {i}", project_paths=[service_path]))
    after = rerank_stats()["queries"]
    assert after >= before + N, f"E4: queries did not rise by {N}: {before} → {after}"


def test_e5_mcp_query_path_no_generation():
    """E5/HR9: MCP query actions contain no generative LLM import (source guard)."""
    base = Path(__file__).parents[2] / "rag_search"
    mcp_src = (base / "server" / "mcp.py").read_text()
    ask_src = (base / "query" / "ask.py").read_text()
    assert "graph.llm" not in mcp_src, "E5: mcp.py imports graph.llm (HR9 violation)"
    assert "import chat" not in mcp_src, "E5: mcp.py imports chat"
    # `assert "run_ask" in mcp_src` stood here. It guarded "the MCP ask handler delegates rather
    # than inlining generation"; `ask` is off the MCP surface, so the subject is gone and the
    # assertion now points at an import mcp.py should not have. run_ask()'s own LLM-freedom is
    # guarded at its new home in test_p14_mcp_readonly.test_run_ask_is_llm_free.
    assert "run_graph" in mcp_src, "E5: mcp.py must delegate to run_graph() (DB-reads helper)"
    assert "graph.llm" not in ask_src, "E5: ask.py imports graph.llm (HR9 violation)"
    assert "def ask(" not in ask_src, "E5: ask.py must not have ask() (was LLM-generative)"


@pytest.mark.costly
def test_e6_dashboard_chat_haiku_only(live_client, service_path):
    """E6/HR10: POST /api/chat_stream streams tokens via claude-haiku-4-5 primary + DeepSeek fallback (codex removed)."""
    r = live_client.post(
        "/api/chat_stream",
        json={"query": "What does this service do?", "project_path": service_path},
        stream=True, timeout=(5, 90),
    )
    assert r.status_code == 200
    tokens, done_seen = [], False
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        try:
            evt = json.loads(line[5:].lstrip())
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "token" and evt.get("text"):
            tokens.append(evt["text"])
        if evt.get("done"):
            done_seen = True
            break
    r.close()
    answer = "".join(tokens)
    assert done_seen, "E6: SSE never sent done:true"
    assert answer, "E6: no tokens received from /api/chat_stream"
    kws = ("promo", "coupon", "discount", "order", "rule", "checkout", "cart", "price", "community")
    assert any(k in answer.lower() for k in kws), f"E6: answer missing service concept: {answer[:200]}"
    src = (Path(__file__).parents[2] / "rag_search" / "server" / "routes_chat.py").read_text()
    assert "QUERY_LLM_MODEL" in src, "E6 guard: routes_chat.py must reference QUERY_LLM_MODEL"
    assert "_ollama_chat" not in src, "E6 guard: routes_chat.py must not have _ollama_chat (no local generative LLM)"
    assert '"codex"' not in src and "shutil.which(\"codex\")" not in src, (
        "E6 guard: codex support must be removed from routes_chat.py"
    )
    assert "--model" in src and "_CLAUDE" in src, (
        "E6 guard: chat must invoke the claude CLI with --model (haiku-only)"
    )


@pytest.mark.costly
def test_e6b_chat_model_is_haiku(live_client, service_path):
    """E6b/HR10: done.model is claude-haiku-4-5 — the only chat model (codex removed).

    Asserts the literal model the daemon serves, not config QUERY_LLM_MODEL (which a stray
    RSE_QUERY_LLM_MODEL env in the test process could shadow); the live daemon is the
    source of truth and is pinned to claude-haiku-4-5 via its systemd drop-in.

    Grounded on a real fixture project because 709b936 stopped spawning `claude -p` at all when
    retrieval cannot ground the question: unscoped, this asked which model answered a request that
    now deliberately never reaches one, and read back `model: ""`. The claim is unchanged — only a
    request that actually reaches the model can testify to which model it is.
    """
    r = live_client.post(
        "/api/chat_stream",
        json={"query": "What is the MCP server name in this engine?",
              "project_path": service_path},
        stream=True, timeout=(5, 90),
    )
    assert r.status_code == 200
    done_evt = None
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        try:
            evt = json.loads(line[5:].lstrip())
        except json.JSONDecodeError:
            continue
        if evt.get("done"):
            done_evt = evt
            break
    r.close()
    assert done_evt is not None, "E6b: SSE never sent done:true"
    assert done_evt.get("model") == "claude-haiku-4-5", (
        f"E6b: chat model must be claude-haiku-4-5 (codex removed); got {done_evt.get('model')!r}"
    )


def test_e7_trimmed_http_surface(live_client):
    """E7/D5: deleted endpoints 404/405; KEEP endpoints 200; route inventory guard."""
    # The tier-3 rows are the second half of this list: R0a deleted the wiki, docgen, okf and
    # kb_health routes, and this is where their absence is now asserted — a hard 404/405, not
    # the `in (400, 404)` acceptance the deleted per-route tests used, which would have stayed
    # green either way.
    deleted = [
        ("GET", "/api/search"), ("POST", "/api/ask"), ("POST", "/api/index"),
        ("POST", "/api/chat"), ("GET", "/api/feature"), ("GET", "/api/service_mesh"),
        ("GET", "/admin/status"),
        ("GET", "/api/wiki"), ("GET", "/api/wiki/page"), ("GET", "/api/wiki/export"),
        ("GET", "/api/wiki_lint"), ("POST", "/api/build_wiki"),
        ("GET", "/api/kb_health"), ("POST", "/api/docgen"), ("POST", "/api/okf"),
        ("GET", "/api/process/bpmn"),
        # events/stream is the last row: the job bus whose only publisher was the pipeline job
        # runner. It is asserted here rather than in test_http_surface/test_sse_contracts because
        # a surviving-but-empty stream still answers 200 with an SSE content-type, so only a
        # 404/405 assertion can tell "deleted" from "publishes nothing".
        ("GET", "/api/events/stream"),
        # The operator-console pass added the last three: the Docs pane's two routes and the
        # question-seeding route behind the "Ask the Codebase" panel. All three answered 200 with
        # real payloads right up to their deletion, so unlike the tier-3 rows above there is no
        # earlier test to inherit from — this is their only absence assertion.
        ("GET", "/api/docs"), ("GET", "/api/docs/page"),
        ("GET", "/api/suggested_questions"),
    ]
    for method, path in deleted:
        r = live_client.request(method, path)
        assert r.status_code in (404, 405), (
            f"E7: {method} {path} → {r.status_code} (expected 404/405 — was deleted)"
        )
    for path in ("/healthz", "/api/projects", "/api/metrics"):
        assert live_client.get(path).status_code == 200, f"E7: {path} not 200 (KEEP broken)"
    chat_router = Path(__file__).parents[2] / "rag_search" / "query" / "chat_router.py"
    assert not chat_router.exists(), "E7 guard: chat_router.py must be deleted"


def test_e8_global_prompt_tool_accuracy():
    """E8: _PROMPT is the canonical body — every registered tool, RESILIENCE, and no drift.

    The tool names are **derived from the live registry**, not written out here. The hand-written
    tuple this replaced (`("search", "ask", "graph", "overview", "index")`) could only ever catch a
    tool deleted from _PROMPT, never one added to the server and never documented — and _PROMPT is
    the entire briefing an MCP client gets. Deriving makes both directions fail
    ([[feedback_allowlist_needs_sufficiency_test]]).

    The count marker is derived for the same reason: `assert "5-tool" in _PROMPT` was a literal
    that had to be remembered, and retiring `ask` is precisely the edit that would have been
    forgotten.
    """
    from rag_search.daemon.global_prompt import _PROMPT
    from rag_search.server.mcp import mcp as _mcp

    names = sorted(t.name for t in asyncio.run(_mcp.list_tools()))
    for tool in names:
        assert tool in _PROMPT, f"E8: _PROMPT does not mention registered tool '{tool}'"
    assert f"{len(names)}-tool" in _PROMPT, (
        f"E8: _PROMPT must state the surface size; {len(names)} tools registered ({names}), "
        f"but '{len(names)}-tool' is absent from the prompt"
    )
    assert "RESILIENCE" in _PROMPT, "E8: _PROMPT missing RESILIENCE rule"
    assert "NEVER auto-index" in _PROMPT, "E8: _PROMPT missing NEVER auto-index rule"
    assert "whenever the current project is indexed" in _PROMPT, (
        "E8: _PROMPT missing 'whenever the current project is indexed'"
    )
    # MCP server instructions must equal _PROMPT (no separate stale copy)
    from rag_search.server.mcp import mcp
    assert mcp.instructions == _PROMPT, "E8: mcp.instructions diverged from _PROMPT"


def test_e8b_shipped_mcp_configs_advertise_the_registered_tools():
    """E8b: the tool names in mcp-config/*.json equal the live registry.

    Same subject as E8 above and the same reason for deriving, one register further out: these
    two files are what a stranger copies to wire the server up, so a name here that the server
    does not expose is a broken install for someone who never sees this repo's tests.

    They drifted exactly as `mcp.py`'s docstring predicts of any mirror nobody consults — both
    still listed `ask` long after it was retired in favour of `overview(what="communities")`,
    and the 2026-07-13 audit had already pulled a phantom env var out of these same two files.
    Deriving from `list_tools()` is what stops the third occurrence.
    """
    from rag_search.server.mcp import mcp as _mcp

    registered = {t.name for t in asyncio.run(_mcp.list_tools())}
    config_dir = Path(__file__).parents[3] / "mcp-config"

    claude_cfg = json.loads((config_dir / "claude-code.json").read_text(encoding="utf-8"))
    advertised = set(claude_cfg["mcpServers"]["rag-search"]["alwaysAllow"])
    # `index` is described but never auto-allowed: it writes, and `index(enabled=False)` deletes.
    # hermes.json has always said so via `always_allowed: false` (asserted below); claude-code.json
    # allow-listed it anyway until 2026-08-05, contradicting both that file and the server's own
    # "NEVER auto-index" instruction. An allow-list is the one place the two must not differ.
    assert advertised == registered - {"index"}, (
        f"mcp-config/claude-code.json allow-lists {sorted(advertised)}; the server registers "
        f"{sorted(registered)}. A name in one and not the other is a config that configures "
        f"nothing, shipped to whoever clones this."
    )

    hermes_cfg = json.loads((config_dir / "hermes.json").read_text(encoding="utf-8"))
    assert set(hermes_cfg["tools"]) == registered, (
        f"mcp-config/hermes.json describes {sorted(hermes_cfg['tools'])}; the server registers "
        f"{sorted(registered)}."
    )
    assert hermes_cfg["tools"]["index"]["always_allowed"] is False, (
        "hermes.json auto-allows `index`; nothing that deletes a project's data may be "
        "pre-approved in a config we ship."
    )

    # README is the third mirror, and it drifted for the same reason the two above did: nothing
    # derived it. It advertised a "5-tool MCP API" over a four-row table that still listed `ask`,
    # retired in favour of overview(what="communities"). A stranger reads the README before
    # either JSON file, so it is the copy that must not lie.
    readme = (Path(__file__).parents[3] / "README.md").read_text(encoding="utf-8")
    section = readme.split("## MCP tool reference", 1)
    assert len(section) == 2, "README.md has no '## MCP tool reference' section to check."
    table = section[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"^\|\s*`([a-z_]+)`\s*\|", table, re.M))
    assert documented == registered, (
        f"README.md documents MCP tools {sorted(documented)}; the server registers "
        f"{sorted(registered)}. Fix the table, not this assertion."
    )
    count_claim = re.search(r"(\d+)-tool MCP API", readme)
    assert count_claim and int(count_claim.group(1)) == len(registered), (
        f"README.md's tool-count claim ({count_claim.group(1) if count_claim else 'missing'}) "
        f"disagrees with the {len(registered)} tools the server registers."
    )


# ── Chat quality: comprehensive question coverage ─────────────────────────────


def _collect_chat_tokens(live_client, question: str, project_path: str, **extra) -> tuple[str, bool]:
    r = live_client.post(
        "/api/chat_stream",
        json={"query": question, "project_path": project_path, **extra},
        stream=True, timeout=(5, 90),
    )
    assert r.status_code == 200
    tokens: list[str] = []
    done_seen = False
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        try:
            evt = json.loads(line[5:].lstrip())
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "token" and evt.get("text"):
            tokens.append(evt["text"])
        if evt.get("done"):
            done_seen = True
            break
    r.close()
    return "".join(tokens), done_seen


@pytest.mark.costly
@pytest.mark.parametrize("question,kws", [
    (
        "What happens when a discount code is applied to an order?",
        ["discount", "coupon", "order", "rule", "promo"],
    ),
    (
        "What functions are involved in validating a coupon code?",
        ["coupon", "valid", "rule", "code", "check"],
    ),
    (
        "How does promo-svc process an incoming order request?",
        ["order", "process", "request", "promo", "service"],
    ),
    (
        "How does promo-svc integrate with checkout or cart services?",
        ["checkout", "cart", "integrat", "service", "federation"],
    ),
])
def test_chat_comprehensive_question_a(live_client, service_path, question, kws):
    """Chat quality A: promo-svc domain questions about discount, coupon, order, integration."""
    answer, done_seen = _collect_chat_tokens(live_client, question, service_path)
    al = answer.lower()
    assert done_seen, f"SSE never sent done for: {question[:60]}"
    assert len(al) > 30, f"Answer too short: {al!r}"
    assert not al.startswith("error"), f"Answer starts with error: {al[:200]!r}"
    assert any(k in al for k in kws), f"Answer missing {kws}: {al[:300]!r}"


@pytest.mark.costly
@pytest.mark.parametrize("question,kws", [
    (
        "What is the overall architecture of this service?",
        ["service", "function", "module", "class", "communit"],
    ),
    (
        "What are the main data models or classes in this service?",
        ["class", "model", "data", "object", "struct"],
    ),
    (
        "What does this service expose and how does it handle errors?",
        ["error", "exception", "return", "handle", "response"],
    ),
    (
        "What business rules does this service implement?",
        ["rule", "business", "logic", "valid", "check"],
    ),
])
def test_chat_comprehensive_question_b(live_client, service_path, question, kws):
    """Chat quality B: structural, model, error-handling, business-rule questions against promo-svc."""
    answer, done_seen = _collect_chat_tokens(live_client, question, service_path)
    al = answer.lower()
    assert done_seen, f"SSE never sent done for: {question[:60]}"
    assert len(al) > 30, f"Answer too short: {al!r}"
    assert not al.startswith("error"), f"Answer starts with error: {al[:200]!r}"
    assert any(k in al for k in kws), f"Answer missing {kws}: {al[:300]!r}"


def test_chat_with_no_project_refuses_instead_of_inventing(live_client):
    """709b936's contract on the branch D2 does not reach: an *absent* project_path.

    This test used to assert the opposite — "empty project_path must still produce answer" — which
    is precisely the ungrounded path 709b936 removed, and it survived that commit only because
    `@slow` deselected it from the default run. Two tests then asserted opposite contracts for
    months. The mark is dropped along with the stale assertion: passing now means no model was
    reached, so it costs a second, and per that commit's own reasoning a gate nothing runs is
    not a gate.

    `_build_context` raises on two distinct branches — no project_path at all, and a project with
    no index. `test_d2_chat_refuses_to_answer_without_context` covers the second one; this covers
    the first. The discriminator is the absence of `token` events, borrowed from D2 for the same
    reason: tokens can only come from the model, and the defect emitted no error at all, so
    asserting an error appears would pass on any change that merely logged while still answering.
    """
    answer, done_seen = _collect_chat_tokens(live_client, "What is a code knowledge base?", "")
    assert done_seen, "the stream must still terminate with done"
    assert answer == "", (
        f"chat answered with no project selected — the ungrounded path 709b936 removed: {answer!r}")


@pytest.fixture(scope="module")
def sse_contract_events(live_client, service_path) -> list[dict]:
    """One real Haiku stream for the two contracts below, which used to post the same body twice.

    The ordering test and the metadata test issued byte-identical requests — same query, same
    project_path — and each drained the stream to read a different part of it. No argument about
    prompt-independence is needed here: the prompts were the same string, so the second
    round-trip re-bought a stream the first already had.
    """
    events: list[dict] = []
    r = live_client.post(
        "/api/chat_stream",
        json={"query": "What does this service do?", "project_path": service_path},
        stream=True, timeout=(5, 60),
    )
    assert r.status_code == 200
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        try:
            evt = json.loads(line[5:].lstrip())
        except json.JSONDecodeError:
            continue
        events.append(evt)
        if evt.get("done"):
            break
    r.close()
    return events


@pytest.mark.costly
def test_chat_sse_event_ordering(sse_contract_events):
    """SSE contract: thinking must be first event, at least one token, done must be last."""
    events = [e.get("type", "") for e in sse_contract_events]
    assert events, "No SSE events received"
    assert events[0] == "thinking", f"First event must be 'thinking', got: {events[:3]}"
    assert "token" in events, f"No token events received: {events}"
    assert events[-1] == "done", f"Last event must be 'done', got: {events[-3:]}"


@pytest.mark.costly
def test_chat_done_event_metadata(sse_contract_events):
    """done event must carry model, elapsed_ms, and non-empty sources for indexed project."""
    done_evt = next((e for e in sse_contract_events if e.get("done")), None)
    assert done_evt is not None, "No done event received"
    from rag_search.core.config import QUERY_LLM_MODEL
    assert done_evt.get("model") == QUERY_LLM_MODEL, f"done.model wrong: {done_evt.get('model')!r}"
    assert isinstance(done_evt.get("elapsed_ms"), int), f"done.elapsed_ms not int: {done_evt}"
    assert isinstance(done_evt.get("sources"), list), f"done.sources not list: {done_evt}"
    # sources may be empty if no chunks matched; field presence and type is what matters


@pytest.mark.costly
def test_chat_multiturn_history_influences_answer(live_client, service_path):
    """Multi-turn: history prepended to prompt makes follow-up context-aware."""
    history = [
        {"role": "user", "content": "Tell me about the promo rule engine."},
        {"role": "assistant", "content": "The promo rule engine validates discount codes and applies coupon rules to orders."},
    ]
    answer, done_seen = _collect_chat_tokens(
        live_client, "What file implements it?", service_path, history=history,
    )
    assert done_seen, "done event never received"
    assert len(answer) > 10, f"Answer too short: {answer!r}"
    al = answer.lower()
    assert any(k in al for k in ["promo", "rule", "coupon", "discount", "file", "implement"]), (
        f"Follow-up must reference promo rule context from history: {al[:300]!r}"
    )
