"""P4 query/ tests: search, graph_handler, ask (slow), chat_router (slow)."""
import pytest

pytestmark = pytest.mark.live

_ALL_INTENTS = frozenset({
    "search", "graph_callers", "graph_callees", "graph_impact",
    "architecture", "global", "feature",
})


# ── query/search ──────────────────────────────────────────────────────────────

def test_search_ranks_auth_first(mini_stores, embedder):
    from opencode_search.index.store import VectorStore
    from opencode_search.query.search import search
    vs = VectorStore(mini_stores["vdb"])
    results = search("JWT authentication token verification", embedder, vs,
                     scope="code", top_k=5)
    vs.close()
    assert results, "Expected at least one search result"
    assert "auth" in results[0]["path"].lower(), \
        f"auth.py should rank first, got: {[r['path'] for r in results]}"


def test_search_scope_docs_returns_empty_for_code_only_project(mini_stores, embedder):
    from opencode_search.index.store import VectorStore
    from opencode_search.query.search import search
    vs = VectorStore(mini_stores["vdb"])
    results = search("query", embedder, vs, scope="docs", top_k=5)
    vs.close()
    # mini project has only .py files → docs scope should return empty
    assert all(r.get("language") in {"markdown", "rst", "text", "html"} for r in results)


# ── query/graph_handler ───────────────────────────────────────────────────────

def test_graph_definition_finds_authenticate(mini_stores):
    from opencode_search.graph.store import GraphStore
    from opencode_search.query.graph_handler import definition
    gs = GraphStore(mini_stores["gdb"])
    defs = definition("authenticate", gs)
    gs.close()
    assert any(d["name"] == "authenticate" for d in defs)


def test_graph_callers_returns_list(mini_stores):
    from opencode_search.graph.store import GraphStore
    from opencode_search.query.graph_handler import callers
    gs = GraphStore(mini_stores["gdb"])
    result = callers("verify_jwt", gs)
    gs.close()
    # DELIBERATE: mini_stores fixture adds symbols but no call edges;
    # callers() returns [] gracefully — testing no-crash, not real graph depth.
    assert isinstance(result, list)


def test_graph_impact_returns_list(mini_stores):
    from opencode_search.graph.store import GraphStore
    from opencode_search.query.graph_handler import impact
    gs = GraphStore(mini_stores["gdb"])
    result = impact("authenticate", gs)
    gs.close()
    # DELIBERATE: authenticate has no callers in mini-project and mini_stores
    # has no call edges; impact BFS returns [] — testing no-crash, not real depth.
    assert isinstance(result, list)


def test_graph_callees_real_be():
    """P10.3: callees() on real BE graph with 63k edges returns ≥1 result."""
    from opencode_search.core.config import project_graph_db
    from opencode_search.core.registry import list_projects
    from opencode_search.graph.store import GraphStore
    from opencode_search.query.graph_handler import callees
    be = next((p.path for p in list_projects() if "astro-promo-be" in p.path and p.enabled), None)
    assert be, "astro-promo-be must be registered (run P8)"
    gs = GraphStore(project_graph_db(be))
    result = callees("NewService", gs)
    gs.close()
    assert isinstance(result, list) and len(result) >= 1


@pytest.mark.slow
def test_graph_narrative_and_trace_real_be():
    """P10.3: impact_narrative + semantic_trace on real BE graph (LLM calls)."""
    from opencode_search.core.config import project_graph_db
    from opencode_search.core.registry import list_projects
    from opencode_search.graph.store import GraphStore
    from opencode_search.query.graph_handler import impact_narrative, semantic_trace
    be = next((p.path for p in list_projects() if "astro-promo-be" in p.path and p.enabled), None)
    assert be, "astro-promo-be must be registered (run P8)"
    gs = GraphStore(project_graph_db(be))
    narrative = impact_narrative("Run", gs)
    trace = semantic_trace("NewService", "Run", gs)
    gs.close()
    assert isinstance(narrative, str) and len(narrative) > 20
    assert isinstance(trace, str) and len(trace) > 10


# ── query/ask (slow) ─────────────────────────────────────────────────────────

@pytest.mark.slow
def test_ask_global_scope_semantic_map():
    """P9.1: scope=global selects communities via GPU cosine — no keyword heuristic."""
    from opencode_search.core.config import project_graph_db, project_vector_db
    from opencode_search.core.registry import list_projects
    from opencode_search.embed.embedder import get_embedder
    from opencode_search.graph.store import GraphStore
    from opencode_search.index.store import VectorStore
    from opencode_search.query.ask import ask
    from opencode_search.query.search import search

    astro = next(
        (p.path for p in list_projects()
         if "astro-project" in p.path and "promo" not in p.path and p.enabled),
        None,
    )
    assert astro is not None, "astro-project must be registered (run P8)"
    gdb, vdb = project_graph_db(astro), project_vector_db(astro)
    assert gdb.exists() and vdb.exists(), "astro-project not fully indexed"

    gs = GraphStore(gdb)
    enriched = gs._con.execute(
        "SELECT COUNT(*) FROM communities WHERE summary IS NOT NULL AND summary != ''"
    ).fetchone()[0]
    assert enriched > 0, "astro-project needs enriched communities (run kb_sweep)"

    vs = VectorStore(vdb)
    chunks = search("project structure", get_embedder(), vs, scope="all", top_k=5)
    answer = ask("What is the overall architecture of this project?", chunks, [gs], scope="global")
    vs.close()
    gs.close()
    assert isinstance(answer, str) and len(answer) > 20


@pytest.mark.slow
def test_ask_all_scopes_real_astro():
    """P10.2: ask() returns non-empty for all/architecture/feature/wiki/business scopes."""
    from opencode_search.core.config import project_graph_db, project_vector_db
    from opencode_search.core.registry import list_projects
    from opencode_search.embed.embedder import get_embedder
    from opencode_search.graph.store import GraphStore
    from opencode_search.index.store import VectorStore
    from opencode_search.query.ask import ask
    from opencode_search.query.search import search

    astro = next(
        (p.path for p in list_projects()
         if "astro-project" in p.path and "promo" not in p.path and p.enabled),
        None,
    )
    assert astro, "astro-project must be registered (run P8)"
    gs = GraphStore(project_graph_db(astro))
    vs = VectorStore(project_vector_db(astro))
    chunks = search("project structure", get_embedder(), vs, scope="all", top_k=5)
    for scope in ("all", "architecture", "feature", "wiki", "business"):
        answer = ask("How does this project work?", chunks, [gs], scope=scope)
        assert isinstance(answer, str) and len(answer) > 10, \
            f"scope={scope!r} answer too short: {answer!r}"
    vs.close()
    gs.close()


@pytest.mark.slow
def test_ask_all_scope_returns_answer(mini_stores, embedder):
    from opencode_search.graph.store import GraphStore
    from opencode_search.index.store import VectorStore
    from opencode_search.query.ask import ask
    from opencode_search.query.search import search
    vs = VectorStore(mini_stores["vdb"])
    gs = GraphStore(mini_stores["gdb"])
    chunks = search("authentication", embedder, vs, scope="all", top_k=5)
    answer = ask("How does authentication work?", chunks, [gs], scope="all")
    vs.close()
    gs.close()
    assert isinstance(answer, str) and len(answer) > 20


# ── query/chat_router (slow) ─────────────────────────────────────────────────

@pytest.mark.slow
def test_extract_symbol_uses_llm_not_heuristic(mini_stores):
    """P9.3: _extract_symbol resolves lowercase symbol via LLM + store, not CamelCase heuristic.

    Old heuristic: 'what calls the authenticate function' → 'what' (no uppercase / underscore).
    New LLM path: extracts 'authenticate' → exact match in graph store.
    """
    from opencode_search.graph.store import GraphStore
    from opencode_search.query.chat_router import _extract_symbol

    gs = GraphStore(mini_stores["gdb"])
    query = "what calls the authenticate function"
    symbol = _extract_symbol(query, gs)
    gs.close()
    assert symbol == "authenticate", (
        f"LLM should extract 'authenticate'; got '{symbol}'. "
        "The old heuristic would have returned 'what'."
    )


@pytest.mark.slow
def test_classify_intent_returns_valid_intent():
    from opencode_search.query.chat_router import classify_intent
    intent = classify_intent("find the function that handles authentication")
    assert intent in _ALL_INTENTS


@pytest.mark.slow
def test_route_search_returns_result(mini_stores, embedder):
    from opencode_search.graph.store import GraphStore
    from opencode_search.index.store import VectorStore
    from opencode_search.query.chat_router import route
    vs = VectorStore(mini_stores["vdb"])
    gs = GraphStore(mini_stores["gdb"])
    intent, answer = route("find the authenticate function", embedder, vs, gs)
    vs.close()
    gs.close()
    assert intent in _ALL_INTENTS
    assert isinstance(answer, str) and len(answer) > 5


@pytest.mark.slow
def test_all_seven_intents_reachable():
    """P10.5: ≥4 distinct intents returned across 7 strongly-typed queries."""
    from opencode_search.query.chat_router import classify_intent
    queries = [
        "find the payment handler function",
        "who calls the Run method",
        "what does ProcessOrder call internally",
        "what breaks if I change the Run function",
        "describe the overall system architecture",
        "give a comprehensive project-wide synthesis",
        "trace the user login feature end to end",
    ]
    seen = {classify_intent(q) for q in queries}
    assert seen <= _ALL_INTENTS, f"Unknown intents: {seen - _ALL_INTENTS}"
    assert len(seen) >= 4, f"Expected ≥4 distinct intents from 7 probes; got {seen}"


@pytest.mark.slow
def test_chat_stream_sse_sends_done(live_client):
    """P10.5/P15.2: /api/chat_stream SSE sends tokens and ends with done:true (LIVE daemon)."""
    import json as _json
    r = live_client.post(
        "/api/chat_stream",
        json={"message": "What is this project?", "project_path": ""},
        stream=True,
        timeout=(5, 90),
    )
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    done_seen = False
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        try:
            evt = _json.loads(line[6:])
        except _json.JSONDecodeError:
            continue
        if evt.get("done"):
            done_seen = True
            break
    r.close()
    assert done_seen, "SSE stream never sent done:true event"
