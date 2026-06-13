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
    assert isinstance(result, list)


def test_graph_impact_returns_list(mini_stores):
    from opencode_search.graph.store import GraphStore
    from opencode_search.query.graph_handler import impact
    gs = GraphStore(mini_stores["gdb"])
    result = impact("authenticate", gs)
    gs.close()
    assert isinstance(result, list)


# ── query/ask (slow) ─────────────────────────────────────────────────────────

@pytest.mark.slow
def test_ask_all_scope_returns_answer(mini_stores, embedder):
    from opencode_search.graph.store import GraphStore
    from opencode_search.index.store import VectorStore
    from opencode_search.query.ask import ask
    from opencode_search.query.search import search
    vs = VectorStore(mini_stores["vdb"])
    gs = GraphStore(mini_stores["gdb"])
    chunks = search("authentication", embedder, vs, scope="all", top_k=5)
    answer = ask("How does authentication work?", chunks, gs, scope="all")
    vs.close()
    gs.close()
    assert isinstance(answer, str) and len(answer) > 20


# ── query/chat_router (slow) ─────────────────────────────────────────────────

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
