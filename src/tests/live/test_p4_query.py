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


# ── query/search: federation fan-out ──────────────────────────────────────────

class _CannedStore:
    """One federation member's two sqlite lanes, canned. Touches no model.

    Not a model fake: there is no embedder, no reranker and no recorded inference here — the
    lanes it stands in for are pure sqlite (vec0 KNN + FTS5), which is exactly why they could be
    priced without a GPU in the first place. What it fakes is *latency*, and that is the point.

    The delay is inverted against position, so the slowest member is first in and last out.
    Completion order is therefore the reverse of input order, which is the only arrangement that
    can tell an order-preserving fan-out from one that consumes completions as they land.
    """

    def __init__(self, name: str, delay: float, cid: int) -> None:
        self.name, self.delay, self.cid = name, delay, cid

    def _rows(self, lane: str) -> list[dict]:
        # Distinct chunk_id per (store, lane): fuse_rrf merges on it, so a collision would fuse
        # two members into one row and quietly shorten the pool this gate counts.
        return [{"chunk_id": self.cid + (0 if lane == "dense" else 1),
                 "path": f"{self.name}/{lane}.py", "text": self.name, "score": 1.0,
                 "start_line": 1, "end_line": 2, "language": "python"}]

    def search(self, q_vec, top_k=8, languages=None):
        import time
        time.sleep(self.delay)
        return self._rows("dense")

    def search_lexical(self, query, top_k=8, languages=None):
        return self._rows("lex")


def test_t4_fanout_preserves_store_order(monkeypatch):
    """T4: the parallel fan-out pools members in *input* order, not completion order.

    `search_federation` fans out over a thread pool because inosoft-project's 157 priced members
    cost 36.68 s of sequential dense KNN. The speedup is only legitimate if the ranking cannot
    move, and what protects the ranking is subtle: `chunks.sort(key=_pool_key)` is *stable*, so
    two members whose chunks tie on RRF score break the tie by pooling position. Preserve input
    order and ties break exactly as the sequential loop broke them; pool by completion order and
    they break by whichever sqlite call happened to finish first — a retrieval change wearing a
    performance change's clothes, and one no recall gate would reliably catch.

    Demonstrated red: swapping `ex.map` for `as_completed` over the same futures returns
    ['s0', 's0', 's1', 's1', 's2', 's2'] against the expected ['s2', 's2', 's1', 's1', 's0', 's0']
    — a full inversion, because the delays are inverted against position. The assertion is on the
    whole order rather than a spot check, so a partial reordering fails it too.

    Touches no model: `_rank` is replaced by identity, so the pooled list is returned before the
    cross-encoder, and the embedder is a constant vector the canned stores ignore.
    """
    import numpy as np

    from rag_search.query import search as search_mod

    monkeypatch.setattr(search_mod, "_rank", lambda query, chunks, top_k: chunks)

    class _Emb:
        def embed(self, texts, batch_size=1):
            return np.zeros((1, 8), dtype="float32")

    stores = [_CannedStore("s2", 0.30, 10), _CannedStore("s1", 0.15, 20),
              _CannedStore("s0", 0.0, 30)]
    out = search_mod.search_federation("q", _Emb(), stores, top_k=8)

    seen = [c["text"] for c in out]
    assert seen == ["s2", "s2", "s1", "s1", "s0", "s0"], (
        f"fan-out pooled in completion order, not input order: {seen}")
