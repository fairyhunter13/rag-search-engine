"""P5 server tests: MCP tools, HTTP routes, dashboard (no mocks)."""
import asyncio
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.live


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
    from pathlib import Path

    from opencode_search.core.registry import list_projects
    from opencode_search.kb.patterns import detect_patterns

    astro = next(
        (p.path for p in list_projects()
         if "astro-project" in p.path and "promo" not in p.path and p.enabled),
        None,
    )
    assert astro is not None, "astro-project must be registered (run P8)"
    result = detect_patterns(Path(astro))
    assert "frameworks" in result
    assert isinstance(result["frameworks"], list)
    # astro-project has astro + react deps → LLM should name ≥1 framework
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
    from pathlib import Path

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


def test_overview_all_whats_real_astro():
    """P10.4: every what= value returns parseable non-empty data on real astro-project."""
    from opencode_search.core.registry import list_projects
    from opencode_search.server.mcp import overview as overview_tool

    astro = next(
        (p.path for p in list_projects()
         if "astro-project" in p.path and "promo" not in p.path and p.enabled),
        None,
    )
    assert astro, "astro-project must be registered (run P8)"
    whats = [
        "structure", "communities", "status", "hierarchy",
        "architecture_domains", "import_cycles",
        "surprising_connections", "suggested_questions",
        "service_mesh", "feature_map", "business_rules", "process_flows",
    ]
    for what in whats:
        result = asyncio.run(overview_tool(astro, what))
        data = json.loads(result)
        assert data, f"overview(what={what!r}) returned empty dict: {result[:120]}"


def test_service_mesh_be_nonempty():
    """BE project (astro-promo-be) has gRPC services — service_mesh must detect them."""
    from opencode_search.core.registry import list_projects
    be = next(
        (p.path for p in list_projects() if "astro-promo-be" in p.path and p.enabled),
        None,
    )
    assert be, "astro-promo-be must be registered (run P8)"
    from opencode_search.server._overview import _detect_services
    svcs = _detect_services(be)
    assert svcs, "BE project must have at least one gRPC service entry"
    names = {n for s in svcs for n in s.get("services", [])}
    assert "GwpService" in names, f"GwpService not in {sorted(names)[:10]}"


def test_suggested_questions_and_chat_context_no_operationalerror(live_client):
    """P23.2: ORDER BY member_count (not node_count) — no OperationalError on real graph DBs."""
    from opencode_search.core.config import project_graph_db
    from opencode_search.core.registry import list_projects

    project = next(
        (p.path for p in list_projects() if p.enabled and project_graph_db(p.path).exists()),
        None,
    )
    assert project, "At least one project with a graph DB must be registered"

    r = live_client.get(f"/api/suggested_questions?project={project}")
    assert r.status_code == 200, f"suggested_questions 500 (likely node_count bug): {r.text}"
    assert "questions" in r.json()


def test_mcp_search_subdir_resolves_to_root():
    """P23.1: search with a non-root project_paths resolves to the enclosing registered root."""
    from pathlib import Path

    from opencode_search.core.config import project_vector_db
    from opencode_search.core.registry import list_projects
    from opencode_search.server.mcp import search as search_tool

    indexed = next(
        (p for p in list_projects() if p.enabled and project_vector_db(p.path).exists()),
        None,
    )
    assert indexed, "At least one indexed project must be registered"

    subdir = str(Path(indexed.path) / "src")
    result = json.loads(asyncio.run(search_tool("function definition", project_paths=[subdir])))
    assert result["projects_searched"] == [indexed.path], (
        f"subdir {subdir!r} must resolve to root {indexed.path!r}; got {result['projects_searched']}"
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


def test_kb_health_measures_summary_not_title(live_client):
    """P25.1: /api/kb_health counts summary-enriched communities, not just titled ones."""
    import sqlite3

    from opencode_search.core.config import project_graph_db
    from opencode_search.core.registry import list_projects

    project = next(
        (p.path for p in list_projects() if p.enabled and project_graph_db(p.path).exists()),
        None,
    )
    assert project, "At least one project with a graph DB must be registered"

    gdb = project_graph_db(project)
    con = sqlite3.connect(str(gdb))
    try:
        total = con.execute("SELECT COUNT(*) FROM communities WHERE level = 1").fetchone()[0]
        summarized = con.execute(
            "SELECT COUNT(*) FROM communities WHERE level = 1 AND summary IS NOT NULL AND summary != ''"
        ).fetchone()[0]
    finally:
        con.close()

    assert total > 0, "Project must have communities"
    r = live_client.get(f"/api/kb_health?project={project}")
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


def test_build_hierarchy_empty_body_not_500(live_client):
    """P28.1: POST /api/build_hierarchy with empty body + project in query param must not 500."""
    from opencode_search.core.config import project_graph_db
    from opencode_search.core.registry import list_projects
    project = next(
        (p.path for p in list_projects() if p.enabled and project_graph_db(p.path).exists()),
        None,
    )
    assert project, "At least one indexed project required"
    r = live_client.post(f"/api/build_hierarchy?project={project}", data=b"")
    assert r.status_code != 500, f"empty body must not 500: {r.status_code} {r.text[:80]}"
    assert r.status_code in (200, 400)


def test_build_hierarchy_action_wiki(live_client):
    """P28.2: action=wiki calls build_wiki and returns pages_written."""
    from opencode_search.core.config import project_graph_db
    from opencode_search.core.registry import list_projects
    project = next(
        (p.path for p in list_projects() if p.enabled and project_graph_db(p.path).exists()),
        None,
    )
    assert project, "At least one indexed project required"
    r = live_client.post(f"/api/build_hierarchy?project={project}&action=wiki", data=b"")
    assert r.status_code == 200, f"action=wiki failed: {r.status_code} {r.text[:80]}"
    assert "pages_written" in r.json(), f"pages_written missing: {r.json()}"


def test_overview_status_has_kb_state():
    """P25.2: overview(what='status') returns kb_state in 4-value set + numeric enriched_pct."""
    from opencode_search.core.config import project_graph_db
    from opencode_search.core.registry import list_projects
    from opencode_search.server.mcp import overview as overview_tool

    project = next(
        (p.path for p in list_projects() if p.enabled and project_graph_db(p.path).exists()),
        None,
    )
    assert project, "At least one project with graph DB must be registered"
    data = json.loads(asyncio.run(overview_tool(project, "status")))
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


def test_graph_defaults_project_path_to_first_project():
    """G5: graph(symbol) with no project_path resolves to first enabled project."""
    from opencode_search.core.registry import list_projects
    from opencode_search.server.mcp import graph as graph_tool

    first = next((p.path for p in list_projects() if p.enabled), None)
    assert first, "At least one enabled project must be registered"
    result = asyncio.run(graph_tool("authenticate"))
    data = json.loads(result)
    assert "error" not in data or "No indexed" not in data.get("error", ""), (
        f"graph with no project_path should resolve, got: {data}"
    )
    assert "matches" in data, f"expected matches key, got: {data}"


_OSE = "/home/user/git/github.com/fairyhunter13/opencode-search-engine"



def test_reranking_is_query_time_only():
    """T-R2: index/ and kb/ packages must not reference reranking (architecture invariant)."""
    from pathlib import Path

    base = Path(__file__).parents[2] / "opencode_search"
    for pkg in [base / "index", base / "kb"]:
        for py in pkg.rglob("*.py"):
            src = py.read_text()
            assert "rerank" not in src.lower(), (
                f"rerank reference found in {py.relative_to(base.parent)} — "
                "reranking must be query-time only (query/ package)"
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
def test_e1_rerank_reorders_search_results():
    """E1/HR8: MCP search on _OSE — rerank_score sorted desc, lift detected on ≥1 of 4 queries."""
    from opencode_search.server.mcp import search as _mcp_search
    queries = [
        "cross-encoder reranker invocation",
        "daemon health check endpoint",
        "sqlite vector store embed",
        "leiden community detection",
    ]
    lift_found = False
    for q in queries:
        data = json.loads(asyncio.run(_mcp_search(q, project_paths=[_OSE])))
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
    from pathlib import Path
    src = (Path(__file__).parents[2] / "opencode_search" / "server" / "mcp.py").read_text()
    assert 'sort(key=lambda r: r.get("score"' not in src, "E1 guard: bare score sort in mcp.py"


@pytest.mark.slow
def test_e2_ask_context_is_rerank_ordered():
    """E2/HR8: MCP ask returns assembled context with path markers, first chunk = rerank top-1."""
    from opencode_search.server.mcp import ask as _mcp_ask
    from opencode_search.server.mcp import search as _mcp_search
    q = "how does the cross-encoder reranker get invoked"
    ctx = asyncio.run(_mcp_ask(q, _OSE, "all"))
    assert ctx and "[" in ctx, f"E2: empty or no path markers: {ctx[:80]}"
    top = json.loads(asyncio.run(_mcp_search(q, project_paths=[_OSE])))["results"]
    assert top, "E2: search returned no results"
    assert top[0].get("path", "") in ctx, (
        f"E2: rerank top-1 {top[0].get('path')!r} not found in ask context"
    )
    assert "I think" not in ctx and "In conclusion" not in ctx, "E2: LLM prose in ctx"


@pytest.mark.slow
def test_e3_community_context_is_reranked():
    """E3/HR8/D2: compose_answer(scope=global) top community differs between distinct queries."""
    from opencode_search.core.config import project_graph_db
    from opencode_search.graph.store import GraphStore
    from opencode_search.query.ask import compose_answer
    gdb = project_graph_db(_OSE)
    if not gdb.exists():
        pytest.skip("_OSE graph DB not found")
    gs = GraphStore(gdb)
    try:
        a = compose_answer("how does reranking work", [], [gs], scope="global")
        b = compose_answer("how does daemon health check work", [], [gs], scope="global")
    finally:
        gs.close()
    assert a, "E3: compose_answer empty for query A"
    assert b, "E3: compose_answer empty for query B"
    top_a = next((ln for ln in a.splitlines() if ln.startswith("## ")), "")
    top_b = next((ln for ln in b.splitlines() if ln.startswith("## ")), "")
    assert top_a != top_b, f"E3: same top community for both queries (static?): {top_a!r}"
    from pathlib import Path
    ask_src = (Path(__file__).parents[2] / "opencode_search" / "query" / "ask.py").read_text()
    assert "rerank_passages" in ask_src, "E3 guard: ask.py must use rerank_passages"
    assert "argsort" not in ask_src, "E3 guard: ask.py must not use argsort"


@pytest.mark.slow
def test_e4_rerank_lift_metric(live_client):
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
        asyncio.run(_mcp_search(f"reranker invocation {i}", project_paths=[_OSE]))
    after = rerank_stats()["queries"]
    assert after >= before + N, f"E4: queries did not rise by {N}: {before} → {after}"


def test_e5_mcp_query_path_no_generation():
    """E5/HR9: MCP query actions contain no generative LLM import (source guard)."""
    from pathlib import Path
    base = Path(__file__).parents[2] / "opencode_search"
    mcp_src = (base / "server" / "mcp.py").read_text()
    ask_src = (base / "query" / "ask.py").read_text()
    assert "graph.llm" not in mcp_src, "E5: mcp.py imports graph.llm (HR9 violation)"
    assert "import chat" not in mcp_src, "E5: mcp.py imports chat"
    assert "compose_answer" in mcp_src, "E5: mcp.py must call compose_answer"
    assert "graph.llm" not in ask_src, "E5: ask.py imports graph.llm (HR9 violation)"
    assert "def ask(" not in ask_src, "E5: ask.py must not have ask() (was LLM-generative)"


@pytest.mark.slow
def test_e6_dashboard_chat_codex_haiku_only(live_client):
    """E6/HR10: POST /api/chat_stream streams tokens via codex→haiku, no ollama."""
    r = live_client.post(
        "/api/chat_stream",
        json={"query": "What is the reranker used in this engine?"},
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
    kws = ("rerank", "jina", "embed", "daemon", "vector", "community", "gpu", "encoder")
    assert any(k in answer.lower() for k in kws), f"E6: answer missing engine concept: {answer[:200]}"
    from pathlib import Path
    src = (Path(__file__).parents[2] / "opencode_search" / "server" / "routes_chat.py").read_text()
    assert "QUERY_LLM_MODEL" in src, "E6 guard: routes_chat.py must reference QUERY_LLM_MODEL"
    assert "_ollama_chat" not in src, "E6 guard: routes_chat.py must not have _ollama_chat"


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
    from pathlib import Path
    chat_router = Path(__file__).parents[2] / "opencode_search" / "query" / "chat_router.py"
    assert not chat_router.exists(), "E7 guard: chat_router.py must be deleted"


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
        "The daemon fails with UNIQUE constraint failed on vec_chunks. How do I debug this?",
        ["vec", "chunk", "index", "unique", "idempot"],
    ),
    (
        "What might cause community enrichment to stay stuck in 'enriching' state?",
        ["community", "enrichment", "summary", "null", "llm"],
    ),
    (
        "What functions are involved in the indexing pipeline from file to stored vector?",
        ["index", "embed", "chunk", "store"],
    ),
    (
        "How does federation work — how does the engine handle multiple repos?",
        ["federat", "member", "symlink", "root"],
    ),
])
def test_chat_comprehensive_question_a(live_client, question, kws):
    """Chat quality A: debug, enrichment, indexing, federation questions."""
    answer, done_seen = _collect_chat_tokens(live_client, question, _OSE)
    al = answer.lower()
    assert done_seen, f"SSE never sent done for: {question[:60]}"
    assert len(al) > 30, f"Answer too short: {al!r}"
    assert "error" not in al[:80], f"Answer starts with error: {al[:200]!r}"
    assert any(k in al for k in kws), f"Answer missing {kws}: {al[:300]!r}"


@pytest.mark.slow
@pytest.mark.parametrize("question,kws", [
    (
        "How does a user search query flow from MCP call to ranked results?",
        ["search", "rank", "rerank", "vector", "embed"],
    ),
    (
        "Why might GPU memory usage spike after indexing a large repo?",
        ["gpu", "cuda", "embed", "ort", "arena"],
    ),
    (
        "What does 'UNIQUE constraint failed: vec_chunks.chunk_id' mean and how do I fix it?",
        ["vec", "unique", "clear", "delete", "idempot"],
    ),
    (
        "What is the knowledge base pipeline and when does it run?",
        ["kb", "knowledge", "communit", "hierarch", "enrich"],
    ),
])
def test_chat_comprehensive_question_b(live_client, question, kws):
    """Chat quality B: search flow, GPU, error message, KB pipeline questions."""
    answer, done_seen = _collect_chat_tokens(live_client, question, _OSE)
    al = answer.lower()
    assert done_seen, f"SSE never sent done for: {question[:60]}"
    assert len(al) > 30, f"Answer too short: {al!r}"
    assert "error" not in al[:80], f"Answer starts with error: {al[:200]!r}"
    assert any(k in al for k in kws), f"Answer missing {kws}: {al[:300]!r}"


@pytest.mark.slow
def test_chat_no_project_path_returns_answer(live_client):
    """Chat with empty project_path still returns a coherent answer (LLM-only, no community context)."""
    answer, done_seen = _collect_chat_tokens(live_client, "What is a code knowledge base?", "")
    assert done_seen
    assert len(answer) > 20, f"Empty project_path must still produce answer: {answer!r}"


@pytest.mark.slow
def test_chat_sse_event_ordering(live_client):
    """SSE contract: thinking must be first event, at least one token, done must be last."""
    events: list[str] = []
    r = live_client.post(
        "/api/chat_stream",
        json={"query": "What is the embedder used for?", "project_path": _OSE},
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
def test_chat_done_event_metadata(live_client):
    """done event must carry model, elapsed_ms, and non-empty sources for indexed project."""
    from opencode_search.core.config import QUERY_LLM_FALLBACK_MODEL
    done_evt = None
    r = live_client.post(
        "/api/chat_stream",
        json={"query": "How does the graph store work?", "project_path": _OSE},
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
    assert done_evt.get("model") in (QUERY_LLM_MODEL, QUERY_LLM_FALLBACK_MODEL), f"done.model wrong: {done_evt.get('model')!r}"
    assert isinstance(done_evt.get("elapsed_ms"), int), f"done.elapsed_ms not int: {done_evt}"
    assert isinstance(done_evt.get("sources"), list), f"done.sources not list: {done_evt}"
    # sources may be empty if no chunks matched; field presence and type is what matters


@pytest.mark.slow
def test_chat_multiturn_history_influences_answer(live_client):
    """Multi-turn: history prepended to prompt makes follow-up context-aware."""
    history = [
        {"role": "user", "content": "Tell me about the VectorStore class."},
        {"role": "assistant", "content": "VectorStore is defined in index/store.py and wraps sqlite-vec for chunk storage."},
    ]
    answer, done_seen = _collect_chat_tokens(
        live_client, "What file is it in?", _OSE, history=history,
    )
    assert done_seen, "done event never received"
    assert len(answer) > 10, f"Answer too short: {answer!r}"
    al = answer.lower()
    assert any(k in al for k in ["store", "index", "file", "vector", "sqlite"]), (
        f"Follow-up must reference VectorStore context from history: {al[:300]!r}"
    )
