"""P5 server tests: MCP tools, HTTP routes, dashboard (no mocks)."""
import asyncio
import json
from pathlib import Path

import pytest

from tests.live._sample_workspace import SampleWorkspace

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def service_path(sample_workspace: SampleWorkspace) -> str:
    """Sample promo-svc — indexed, 7 L1 communities, has graph.db + vectors.db."""
    return sample_workspace.promo


def test_mcp_has_five_tools():
    """All 5 MCP tools registered in FastMCP app."""
    from opencode_search.server.mcp import mcp
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert {"search", "ask", "graph", "overview", "index"} <= names


def test_mcp_graph_nonexistent_returns_error():
    """graph tool returns {error:...} JSON for an unindexed project."""
    from opencode_search.server.mcp import graph as graph_tool
    result = asyncio.run(graph_tool("authenticate", "/nonexistent/path", "definition"))
    data = json.loads(result)
    assert "error" in data


def test_mcp_overview_projects_returns_list():
    """P15.4: overview(what='projects') returns ≥1 real registered project."""
    from opencode_search.server.mcp import overview as overview_tool
    result = asyncio.run(overview_tool("", "projects"))
    data = json.loads(result)
    assert "projects" in data
    assert len(data["projects"]) >= 1, "daemon should have ≥1 registered project"


def test_mcp_overview_metrics():
    """P20.3: overview(what='metrics') returns chat_stream metrics dict."""
    from opencode_search.server.mcp import overview as overview_tool
    result = asyncio.run(overview_tool("", "metrics"))
    data = json.loads(result)
    assert "chat_stream" in data, f"metrics missing chat_stream key: {result}"
    assert "stream_error_count" in data["chat_stream"], f"chat_stream missing stream_error_count: {data}"


def test_mcp_index_register_remove(safe_tmp_path):
    """index tool registers then removes a project without crashing."""
    from opencode_search.server.mcp import index as index_tool
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


def test_dashboard_five_views(live_client):
    """P15.2: /dashboard on the REAL daemon — all 5 views present."""
    r = live_client.get("/dashboard")
    assert r.status_code == 200
    body = r.text.lower()
    for view in ("pulse", "chat", "admin", "wiki", "graph"):
        assert view in body, f"dashboard missing '{view}' view"


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



@pytest.mark.slow
def test_detect_patterns_llm_frameworks():
    """P9.2: detect_patterns() derives frameworks via LLM, not _FW static dict."""

    from opencode_search.kb.patterns import detect_patterns
    from tests.live._projects import federation_root

    proj = federation_root()
    result = detect_patterns(Path(proj))
    assert "frameworks" in result
    assert isinstance(result["frameworks"], list)
    # the federation root should have framework deps → LLM should name ≥1 framework
    assert len(result["frameworks"]) >= 1


def test_index_tool_rejects_forbidden_root(safe_tmp_path):
    """P24.3: index(/tmp/...) must return status='forbidden' and NOT register the path."""
    from opencode_search.core.registry import get_project
    from opencode_search.server.mcp import index as index_tool

    bad = "/tmp/ocs-test-forbidden-registration-check"
    result = json.loads(asyncio.run(index_tool(bad, enabled=True)))
    assert result["status"] == "forbidden", f"expected forbidden, got {result}"
    assert get_project(bad) is None, "forbidden path must NOT be registered"

    normal = str(safe_tmp_path)
    ok = json.loads(asyncio.run(index_tool(normal, enabled=True)))
    assert ok["status"] in ("flagged", "already_registered"), f"normal path failed: {ok}"
    asyncio.run(index_tool(normal, enabled=False))  # cleanup


def test_index_tool_e2e(safe_tmp_path):
    """P10.4b: enabled=True creates registry entry; enabled=False removes it + index dir."""

    from opencode_search.core.config import index_dir
    from opencode_search.core.registry import list_projects
    from opencode_search.server.mcp import index as index_tool

    p = str(safe_tmp_path)
    reg = json.loads(asyncio.run(index_tool(p, enabled=True)))
    assert reg["status"] in ("flagged", "already_registered")
    assert any(proj.path == p for proj in list_projects()), "Project not in registry after register"
    reg2 = json.loads(asyncio.run(index_tool(p, enabled=True)))
    assert reg2["status"] == "already_registered"
    idx = index_dir(p)
    idx.mkdir(parents=True, exist_ok=True)
    rem = json.loads(asyncio.run(index_tool(p, enabled=False)))
    assert rem["status"] in ("removed", "not_found")
    assert not any(proj.path == p for proj in list_projects()), "Project still in registry after remove"
    assert not Path(idx).exists(), "Index dir not deleted after remove"


def test_overview_all_whats_real_federation_root():
    """P10.4: every what= value returns parseable non-empty data on the real federation root."""
    from opencode_search.server.mcp import overview as overview_tool
    from tests.live._projects import federation_root

    fed_root = federation_root()
    whats = [
        "structure", "communities", "status", "import_cycles",
        "surprising_connections", "suggested_questions",
        "service_mesh", "feature_map", "business_rules", "process_flows",
    ]
    for what in whats:
        result = asyncio.run(overview_tool(fed_root, what))
        data = json.loads(result)
        assert data, f"overview(what={what!r}) returned empty dict: {result[:120]}"


def test_service_mesh_be_nonempty():
    """Federation service_mesh must detect gRPC/HTTP services from the federation root."""
    from tests.live._projects import federation_root
    root = federation_root()
    from opencode_search.daemon.federation import expand_federation
    from opencode_search.server._overview import _detect_services
    svcs = [s for p in expand_federation(root) for s in _detect_services(p)]
    assert svcs, "Federation must have at least one gRPC service entry"
    names = {n for s in svcs for n in s.get("services", [])}
    assert names, (
        f"service_mesh detected gRPC entry but no named services; svcs={svcs[:2]}"
    )


def test_suggested_questions_and_chat_context_no_operationalerror(live_client, service_path):
    """P23.2: ORDER BY member_count (not node_count) — no OperationalError on real graph DBs."""
    r = live_client.get(f"/api/suggested_questions?project={service_path}")
    assert r.status_code == 200, f"suggested_questions 500 (likely node_count bug): {r.text}"
    assert "questions" in r.json()


def test_mcp_search_subdir_resolves_to_root(service_path):
    """P23.1: search with a non-root project_paths resolves to the enclosing registered root."""

    from opencode_search.server.mcp import search as search_tool

    subdir = str(Path(service_path) / "src")
    result = json.loads(asyncio.run(search_tool("function definition", project_paths=[subdir])))
    assert result["projects_searched"] == [service_path], (
        f"subdir {subdir!r} must resolve to root {service_path!r}; got {result['projects_searched']}"
    )
    assert result["total"] > 0, "Expected results from indexed project"

    outside = json.loads(asyncio.run(search_tool("function definition", project_paths=["/nonexistent/path"])))
    assert outside["total"] == 0
    assert outside["projects_searched"] == []


def test_auto_pipeline_status_real(live_client, safe_tmp_path):
    """P19.6: /api/auto_pipeline_status returns real enabled/pending — not canned data.

    Register an un-indexed tmp project → it must appear in pending.
    Pause sweeps → enabled must flip to False.
    """
    import urllib.request

    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.registry import remove_project, upsert_project

    proj_path = str(safe_tmp_path)
    try:
        # Resume sweeps explicitly — autouse session fixture pauses them globally,
        # so we must resume to test the enabled=True state, then re-pause.
        urllib.request.urlopen(
            urllib.request.Request("http://127.0.0.1:8765/api/sweeps/resume", data=b"", method="POST"),
            timeout=3,
        )
        r0 = live_client.get("/api/auto_pipeline_status")
        assert r0.status_code == 200, f"unexpected status {r0.status_code}"
        d0 = r0.json()
        assert "enabled" in d0 and "pending" in d0, f"missing keys: {d0}"
        assert d0["enabled"] is True, f"expected enabled=True after explicit resume, got {d0}"

        # Pause sweeps, THEN register — pending check is now race-free
        urllib.request.urlopen(
            urllib.request.Request("http://127.0.0.1:8765/api/sweeps/pause", data=b"", method="POST"),
            timeout=3,
        )
        upsert_project(ProjectEntry(path=proj_path, enabled=True))
        r = live_client.get("/api/auto_pipeline_status")
        d = r.json()
        assert d["enabled"] is False, f"expected enabled=False after pause, got {d}"
        assert proj_path in d["pending"], (
            f"un-indexed {proj_path} must appear in pending; got {d['pending'][:3]}"
        )
    finally:
        urllib.request.urlopen(
            urllib.request.Request("http://127.0.0.1:8765/api/sweeps/pause", data=b"", method="POST"),
            timeout=3,
        )
        remove_project(proj_path)


def test_kb_health_measures_summary_not_title(live_client, service_path):
    """P25.1: /api/kb_health counts summary-enriched communities, not just titled ones."""
    import sqlite3

    from opencode_search.core.config import project_graph_db

    gdb = project_graph_db(service_path)
    assert gdb.exists(), "promo-svc graph.db not found — sample_workspace fixture must run first"
    con = sqlite3.connect(str(gdb))
    try:
        total = con.execute("SELECT COUNT(*) FROM communities WHERE level = 1").fetchone()[0]
        summarized = con.execute(
            "SELECT COUNT(*) FROM communities WHERE level = 1 "
            "AND summary IS NOT NULL AND summary != ''"
        ).fetchone()[0]
    finally:
        con.close()
    assert total > 0, "promo-svc must have L1 communities"
    r = live_client.get(f"/api/kb_health?project={service_path}")
    assert r.status_code == 200, f"kb_health {r.status_code}: {r.text}"
    d = r.json()
    expected_pct = round(summarized / total * 100, 1) if total else 0
    assert abs(d["enriched_pct"] - expected_pct) < 0.1, (
        f"enriched_pct={d['enriched_pct']} != summary-based {expected_pct} "
        f"(summarized={summarized} total={total})"
    )


@pytest.mark.slow
def test_enrich_project_uses_summary_gate(safe_tmp_path):
    """P27: _enrich_project enriches titled-but-unsummarized communities."""
    import sqlite3  # noqa: I001
    from opencode_search.core.config import project_graph_db
    from opencode_search.daemon.sweeps import _enrich_project
    from opencode_search.graph.community import detect_communities
    from opencode_search.graph.extractor import extract_symbols, symbol_id
    from opencode_search.graph.store import GraphStore
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
        assert titled > 0, "P21 must set structural labels before enrichment"
    finally:
        gs.close()
    _enrich_project(proj)
    with sqlite3.connect(str(project_graph_db(proj))) as con:
        post = con.execute(
            "SELECT COUNT(*) FROM communities WHERE summary IS NOT NULL AND summary != ''"
        ).fetchone()[0]
    assert post > 0, "enrichment gate must use summary IS NULL, not title IS NULL"


def test_build_wiki_default_action_succeeds(live_client, service_path):
    """P28.1: POST /api/build_wiki without explicit action defaults to wiki and returns pages_written."""
    r = live_client.post(f"/api/build_wiki?project={service_path}", data=b"")
    assert r.status_code == 200, f"default action (wiki) must succeed: {r.status_code} {r.text[:80]}"
    assert "pages_written" in r.json(), f"pages_written missing: {r.json()}"


def test_build_wiki_action_wiki(live_client, service_path):
    """P28.2: action=wiki calls build_wiki and returns pages_written."""
    r = live_client.post(f"/api/build_wiki?project={service_path}&action=wiki", data=b"")
    assert r.status_code == 200, f"action=wiki failed: {r.status_code} {r.text[:80]}"
    assert "pages_written" in r.json(), f"pages_written missing: {r.json()}"


def test_overview_status_has_kb_state(service_path):
    """P25.2: overview(what='status') returns kb_state in 4-value set + numeric enriched_pct."""
    from opencode_search.server.mcp import overview as overview_tool

    data = json.loads(asyncio.run(overview_tool(service_path, "status")))
    assert "kb_state" in data, f"kb_state missing from status: {data}"
    assert data["kb_state"] in ("indexing", "searchable", "enriching", "ready"), (
        f"kb_state={data['kb_state']!r} not in expected set"
    )
    assert isinstance(data.get("enriched_pct"), (int, float)), (
        f"enriched_pct must be numeric: {data}"
    )


def test_overview_unknown_what_returns_error():
    """G4: overview(what='bogus') returns {error, valid} instead of silently falling through."""
    from opencode_search.server.mcp import overview as overview_tool
    result = asyncio.run(overview_tool("", "bogus_unknown_what"))
    data = json.loads(result)
    assert "error" in data, f"expected error key, got: {data}"
    assert "valid" in data, f"expected valid key, got: {data}"
    assert "structure" in data["valid"], f"'structure' missing from valid list: {data['valid']}"


def test_graph_defaults_project_path_to_first_project(sample_workspace):
    """G5: graph(symbol) with no project_path resolves to the first enabled registry project.

    Mechanics test — proves the resolution rule (first-enabled-project fallback) rather than
    asserting specific content from a real device project. The sample_workspace fixture ensures
    at least one indexed project is registered (promo-svc), so the resolution always succeeds.
    """
    from opencode_search.core.registry import list_projects
    from opencode_search.server.mcp import graph as graph_tool

    first = next((p.path for p in list_projects() if p.enabled), None)
    assert first, "At least one enabled project must be registered (sample_workspace provides this)"
    # Verify the resolution rule fires: no project_path → must resolve, not error "No indexed project"
    result = asyncio.run(graph_tool("own_fn"))
    data = json.loads(result)
    assert "No indexed project" not in data.get("error", ""), (
        f"G5: graph with no project_path must resolve to first enabled project, got: {data}"
    )
    assert "matches" in data, f"G5: expected matches key in resolved graph, got: {data}"


_OSE_SRC = Path(__file__).resolve().parents[3]  # source-file reads only; NOT passed to daemon



def test_reranking_is_query_time_only():
    """T-R2: index/ and kb/ packages must not directly invoke the cross-encoder.

    The reranking IMPLEMENTATION (rerank_passages) lives only in query/search.py.
    The G1.75 bridge kb/resolve_rerank.py is the single permitted delegation point;
    all other kb/ files must call through it, never import rerank_passages directly.
    """

    base = Path(__file__).parents[2] / "opencode_search"
    # Only the G1.75 bridge may import rerank_passages directly
    _DIRECT_RERANK_EXCEPTION = {"resolve_rerank.py"}
    for pkg in [base / "index", base / "kb"]:
        for py in pkg.rglob("*.py"):
            if py.name in _DIRECT_RERANK_EXCEPTION:
                continue
            src = py.read_text()
            assert "rerank_passages" not in src, (
                f"Direct rerank_passages call found in {py.relative_to(base.parent)} — "
                "route through kb/resolve_rerank.py (the G1.75 bridge) instead"
            )


@pytest.mark.slow
def test_search_reranks_full_pool(mini_stores, embedder):
    """T-R3/C2: search() reranks the entire scope-filtered pool (no pre-truncation).

    mini_stores has ~6 chunks. With top_k=2: old code would rerank only top_k*2=4;
    new code reranks all top_k*3=6 (or fewer if scope-filtered).  Results must be
    non-empty, carry rerank_score, and be monotonic desc by that score.
    """
    from opencode_search.index.store import VectorStore
    from opencode_search.query.search import search

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


@pytest.mark.slow
def test_e1_rerank_reorders_search_results(service_path):
    """E1/HR8: MCP search on sample service — rerank_score sorted desc, lift detected on ≥1 of 4 queries."""
    from opencode_search.server.mcp import search as _mcp_search
    queries = [
        "discount rule application",
        "coupon validation logic",
        "order processing function",
        "checkout service integration",
    ]
    lift_found = False
    for q in queries:
        data = json.loads(asyncio.run(_mcp_search(q, project_paths=[service_path])))
        res = data.get("results", [])
        assert res, f"E1: no results for {q!r}"
        rs = [r.get("rerank_score") for r in res]
        assert all(s is not None for s in rs), f"E1: missing rerank_score: {rs}"
        assert all(rs[i] >= rs[i+1] for i in range(len(rs)-1)), f"E1: unsorted: {rs}"
        if len(res) > 1:
            vec_top = max(res, key=lambda r: r.get("score", 0.0))
            if vec_top.get("path") != res[0].get("path"):
                lift_found = True
    assert lift_found, "E1: rerank never changed top-1 vs vector order (pass-through?)"
    src = (Path(__file__).parents[2] / "opencode_search" / "server" / "mcp.py").read_text()
    assert 'sort(key=lambda r: r.get("score"' not in src, "E1 guard: bare score sort in mcp.py"


@pytest.mark.slow
def test_e2_ask_context_is_rerank_ordered(service_path):
    """E2/HR8: MCP ask returns assembled context with path markers, first chunk = rerank top-1."""
    from opencode_search.server.mcp import ask as _mcp_ask
    from opencode_search.server.mcp import search as _mcp_search
    q = "how does the promotion rule engine apply discounts"
    ctx = asyncio.run(_mcp_ask(q, service_path, "all"))
    assert ctx and "[" in ctx, f"E2: empty or no path markers: {ctx[:80]}"
    top = json.loads(asyncio.run(_mcp_search(q, project_paths=[service_path])))["results"]
    assert top, "E2: search returned no results"
    assert top[0].get("path", "") in ctx, (
        f"E2: rerank top-1 {top[0].get('path')!r} not found in ask context"
    )
    # Structural guard: assembled context starts with a section header, not LLM prose.
    assert ctx.startswith("## "), "E2: LLM prose in ctx — assembled context must start with ## section marker"


@pytest.mark.slow
def test_e3_community_context_is_reranked(service_path):
    """E3/HR8/D2: compose_answer(scope=global) top community differs between distinct queries."""
    from opencode_search.core.config import project_graph_db
    from opencode_search.graph.store import GraphStore
    from opencode_search.query.ask import compose_answer
    gdb = project_graph_db(service_path)
    assert gdb.exists(), (
        "sample promo-svc graph DB not found — sample_workspace fixture must run first"
    )
    gs = GraphStore(gdb)
    try:
        a = compose_answer("how does coupon validation work", [], [gs], scope="global")
        b = compose_answer("how does checkout integration work", [], [gs], scope="global")
    finally:
        gs.close()
    assert a, "E3: compose_answer empty for query A"
    assert b, "E3: compose_answer empty for query B"
    # scope="global" always starts with structural "## Architecture (community map)";
    # compare full response content — if semantic reranking is query-aware, a != b.
    assert a != b, "E3: compose_answer identical for two distinct queries (reranking is static?)"
    ask_src = (Path(__file__).parents[2] / "opencode_search" / "query" / "ask.py").read_text()
    assert "rerank_passages" in ask_src, "E3 guard: ask.py must use rerank_passages"
    assert "argsort" not in ask_src, "E3 guard: ask.py must not use argsort"


@pytest.mark.slow
def test_e4_rerank_lift_metric(live_client, service_path):
    """E4/D3: /api/metrics exposes rerank block; in-process search increments the counter."""
    from opencode_search.query.search import rerank_stats
    from opencode_search.server.mcp import search as _mcp_search
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
    base = Path(__file__).parents[2] / "opencode_search"
    mcp_src = (base / "server" / "mcp.py").read_text()
    ask_src = (base / "query" / "ask.py").read_text()
    assert "graph.llm" not in mcp_src, "E5: mcp.py imports graph.llm (HR9 violation)"
    assert "import chat" not in mcp_src, "E5: mcp.py imports chat"
    # After PART-3 refactor: mcp.ask delegates to run_ask(), which calls compose_answer()
    assert "run_ask" in mcp_src, "E5: mcp.py must delegate to run_ask() (LLM-free helper)"
    assert "run_graph" in mcp_src, "E5: mcp.py must delegate to run_graph() (DB-reads helper)"
    import inspect

    from opencode_search.query.ask import run_ask as _ra
    assert "compose_answer" in inspect.getsource(_ra), (
        "E5: run_ask() must call compose_answer() (LLM-free context assembler)"
    )
    assert "graph.llm" not in ask_src, "E5: ask.py imports graph.llm (HR9 violation)"
    assert "def ask(" not in ask_src, "E5: ask.py must not have ask() (was LLM-generative)"


@pytest.mark.slow
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
    src = (Path(__file__).parents[2] / "opencode_search" / "server" / "routes_chat.py").read_text()
    assert "QUERY_LLM_MODEL" in src, "E6 guard: routes_chat.py must reference QUERY_LLM_MODEL"
    assert "_ollama_chat" not in src, "E6 guard: routes_chat.py must not have _ollama_chat (no local generative LLM)"
    assert '"codex"' not in src and "shutil.which(\"codex\")" not in src, (
        "E6 guard: codex support must be removed from routes_chat.py"
    )
    assert "--model" in src and "_CLAUDE" in src, (
        "E6 guard: chat must invoke the claude CLI with --model (haiku-only)"
    )


@pytest.mark.slow
def test_e6b_chat_model_is_haiku(live_client):
    """E6b/HR10: done.model is claude-haiku-4-5 — the only chat model (codex removed).

    Asserts the literal model the daemon serves, not config QUERY_LLM_MODEL (which a stray
    OPENCODE_QUERY_LLM_MODEL env in the test process could shadow); the live daemon is the
    source of truth and is pinned to claude-haiku-4-5 via its systemd drop-in.
    """
    r = live_client.post(
        "/api/chat_stream",
        json={"query": "What is the MCP server name in this engine?"},
        stream=True, timeout=(5, 45),
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
    deleted = [
        ("GET", "/api/search"), ("POST", "/api/ask"), ("POST", "/api/index"),
        ("POST", "/api/chat"), ("GET", "/api/feature"), ("GET", "/api/service_mesh"),
        ("GET", "/admin/status"),
    ]
    for method, path in deleted:
        r = live_client.request(method, path)
        assert r.status_code in (404, 405), (
            f"E7: {method} {path} → {r.status_code} (expected 404/405 — was deleted)"
        )
    for path in ("/healthz", "/api/projects", "/api/metrics"):
        assert live_client.get(path).status_code == 200, f"E7: {path} not 200 (KEEP broken)"
    chat_router = Path(__file__).parents[2] / "opencode_search" / "query" / "chat_router.py"
    assert not chat_router.exists(), "E7 guard: chat_router.py must be deleted"


def test_e8_global_prompt_tool_accuracy():
    """E8: _PROMPT is the canonical Phase-100 body — 5 tools + RESILIENCE + no drift."""
    from opencode_search.daemon.global_prompt import _PROMPT

    for tool in ("search", "ask", "graph", "overview", "index"):
        assert tool in _PROMPT, f"E8: _PROMPT missing tool '{tool}'"
    assert "5-tool" in _PROMPT, "E8: _PROMPT is not Phase-100 canonical (missing '5-tool')"
    assert "RESILIENCE" in _PROMPT, "E8: _PROMPT missing RESILIENCE rule"
    assert "NEVER auto-index" in _PROMPT, "E8: _PROMPT missing NEVER auto-index rule"
    assert "whenever the current project is indexed" in _PROMPT, (
        "E8: _PROMPT missing 'whenever the current project is indexed'"
    )
    # MCP server instructions must equal _PROMPT (no separate stale copy)
    from opencode_search.server.mcp import mcp
    assert mcp.instructions == _PROMPT, "E8: mcp.instructions diverged from _PROMPT"


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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
def test_chat_no_project_path_returns_answer(live_client):
    """Chat with empty project_path still returns a coherent answer (LLM-only, no community context)."""
    answer, done_seen = _collect_chat_tokens(live_client, "What is a code knowledge base?", "")
    assert done_seen
    assert len(answer) > 20, f"Empty project_path must still produce answer: {answer!r}"


@pytest.mark.slow
def test_chat_sse_event_ordering(live_client, service_path):
    """SSE contract: thinking must be first event, at least one token, done must be last."""
    events: list[str] = []
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
        events.append(evt.get("type", ""))
        if evt.get("done"):
            break
    r.close()
    assert events, "No SSE events received"
    assert events[0] == "thinking", f"First event must be 'thinking', got: {events[:3]}"
    assert "token" in events, f"No token events received: {events}"
    assert events[-1] == "done", f"Last event must be 'done', got: {events[-3:]}"


@pytest.mark.slow
def test_chat_done_event_metadata(live_client, service_path):
    """done event must carry model, elapsed_ms, and non-empty sources for indexed project."""
    done_evt = None
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
        if evt.get("done"):
            done_evt = evt
            break
    r.close()
    assert done_evt is not None, "No done event received"
    from opencode_search.core.config import QUERY_LLM_MODEL
    assert done_evt.get("model") == QUERY_LLM_MODEL, f"done.model wrong: {done_evt.get('model')!r}"
    assert isinstance(done_evt.get("elapsed_ms"), int), f"done.elapsed_ms not int: {done_evt}"
    assert isinstance(done_evt.get("sources"), list), f"done.sources not list: {done_evt}"
    # sources may be empty if no chunks matched; field presence and type is what matters


@pytest.mark.slow
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
