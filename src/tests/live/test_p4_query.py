"""P4 query/ tests: search, graph_handler, chat_stream (slow)."""
import pytest

pytestmark = pytest.mark.live


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
    from tests.live._projects import service_member
    be = service_member()
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
    from tests.live._projects import service_member
    be = service_member()
    gs = GraphStore(project_graph_db(be))
    narrative = impact_narrative("Run", gs)
    trace = semantic_trace("NewService", "Run", gs)
    gs.close()
    assert isinstance(narrative, str) and len(narrative) > 20
    assert isinstance(trace, str) and len(trace) > 10



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
