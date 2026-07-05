"""P4 query/ tests: search, graph_handler, chat_stream (slow)."""
import pytest

pytestmark = pytest.mark.live


# ── query/search ──────────────────────────────────────────────────────────────

def test_search_ranks_auth_first(mini_stores, embedder):
    from rag_search.index.store import VectorStore
    from rag_search.query.search import search
    vs = VectorStore(mini_stores["vdb"])
    results = search("JWT authentication token verification", embedder, vs,
                     scope="code", top_k=5)
    vs.close()
    assert results, "Expected at least one search result"
    assert "auth" in results[0]["path"].lower(), \
        f"auth.py should rank first, got: {[r['path'] for r in results]}"


def test_search_scope_docs_returns_empty_for_code_only_project(mini_stores, embedder):
    from rag_search.index.store import VectorStore
    from rag_search.query.search import search
    vs = VectorStore(mini_stores["vdb"])
    results = search("query", embedder, vs, scope="docs", top_k=5)
    vs.close()
    # mini project has only .py files → docs scope should return empty
    assert all(r.get("language") in {"markdown", "rst", "text", "html"} for r in results)


# ── query/graph_handler ───────────────────────────────────────────────────────

def test_graph_definition_finds_authenticate(mini_stores):
    from rag_search.graph.store import GraphStore
    from rag_search.query.graph_handler import definition
    gs = GraphStore(mini_stores["gdb"])
    defs = definition("authenticate", gs)
    gs.close()
    assert any(d["name"] == "authenticate" for d in defs)


def test_graph_callers_returns_list(mini_stores):
    from rag_search.graph.store import GraphStore
    from rag_search.query.graph_handler import callers
    gs = GraphStore(mini_stores["gdb"])
    result = callers("verify_jwt", gs)
    gs.close()
    # DELIBERATE: mini_stores fixture adds symbols but no call edges;
    # callers() returns [] gracefully — testing no-crash, not real graph depth.
    assert isinstance(result, list)


def test_graph_impact_returns_list(mini_stores):
    from rag_search.graph.store import GraphStore
    from rag_search.query.graph_handler import impact
    gs = GraphStore(mini_stores["gdb"])
    result = impact("authenticate", gs)
    gs.close()
    # DELIBERATE: authenticate has no callers in mini-project and mini_stores
    # has no call edges; impact BFS returns [] — testing no-crash, not real depth.
    assert isinstance(result, list)


def test_graph_callees_real_be(sample_workspace):
    """P10.3: callees() on sample service member graph returns ≥1 result for an edge-connected fn."""
    import sqlite3

    from rag_search.core.config import project_graph_db
    from rag_search.graph.store import GraphStore
    from rag_search.query.graph_handler import callees
    gdb = project_graph_db(sample_workspace.promo)
    with sqlite3.connect(str(gdb)) as con:
        row = con.execute(
            "SELECT s.name FROM symbols s JOIN edges e ON e.caller_sid=s.sid LIMIT 1"
        ).fetchone()
    assert row is not None, "sample promo-svc must have ≥1 edge in graph"
    gs = GraphStore(gdb)
    result = callees(row[0], gs)
    gs.close()
    assert isinstance(result, list) and len(result) >= 1




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
