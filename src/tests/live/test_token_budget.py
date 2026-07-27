"""TK1-TK3: gates for the July-2026 token-efficiency pass (plan Phase 1).

Each fails on the pre-Phase-1 code, which is what makes it a gate rather than decoration:
TK1 on a tool that had no verbosity knob and always returned bodies, TK2 on the per-project
loop that embedded the query once per federation member, TK3 on the scope post-filter that
returned a remnant of the pool instead of a full one.
"""
import json

import pytest

pytestmark = pytest.mark.live

_TOP_K = 8

# A compact hit is five short fields plus a <=200-char preview, so eight of them plus the
# envelope have no honest way to reach a kilobyte apiece. The fleet's mean chunk body is
# 838 chars, so `full` for the same query clears this ceiling on content alone.
_COMPACT_BYTES_MAX = 4096


def test_tk1_compact_response_fits_a_byte_budget(sample_workspace):
    """TK1: compact must be small in absolute terms, not merely smaller than full.

    "compact < full" would pass on a one-character difference, so the ceiling is the real
    assertion; the ratio and the absent bodies corroborate it.
    """
    from rag_search.server.mcp import _search_sync

    q = "apply a discount to the cart"
    paths = [sample_workspace.promo]
    compact = _search_sync(q, "all", paths, _TOP_K, "compact")
    full = _search_sync(q, "all", paths, _TOP_K, "full")
    hits = json.loads(compact)["results"]
    assert hits, "TK1: query matched nothing — the budget assertion would be vacuous"
    assert len(compact) < _COMPACT_BYTES_MAX, (
        f"TK1: compact response is {len(compact)} bytes, over the {_COMPACT_BYTES_MAX} ceiling"
    )
    assert len(compact) * 2 < len(full), (
        f"TK1: compact ({len(compact)}B) is not materially smaller than full ({len(full)}B)"
    )
    assert all("content" not in h for h in hits), "TK1: compact results still carry chunk bodies"
    assert all(h["path"] and h["start_line"] for h in hits), (
        "TK1: compact dropped bodies without leaving a location to Read"
    )


def test_tk2_one_embed_for_a_whole_federation(federation_root_path):
    """TK2: a federated search embeds the query once, not once per member.

    A latency assertion would not discriminate here — N embeds of one short string are fast
    enough to hide the defect — so this counts the real calls the GPU actually served.
    """
    from rag_search.embed.embedder import embed_stats
    from rag_search.server.mcp import _search_sync

    before = embed_stats()["calls"]
    payload = json.loads(
        _search_sync("checkout total", "all", [federation_root_path], _TOP_K, "compact")
    )
    spent = embed_stats()["calls"] - before
    members = len(payload["projects_searched"])
    assert members >= 2, f"TK2 needs a multi-member federation to discriminate, got {members}"
    assert spent == 1, f"TK2: {spent} query embeds for {members} members — expected exactly 1"


def test_tk3_docs_scope_returns_a_full_pool(embedder, safe_tmp_path):
    """TK3: scope must narrow the corpus, not the result set.

    In a code-heavy store the matching prose sits far past a code-ranked top_k*3 window, so a
    post-filter hands back whatever survives. Asserting "some docs came back" would pass on
    that remnant; asserting a full top_k is what fails on it.
    """
    from rag_search.index.store import VectorStore
    from rag_search.query.search import search

    code = [f"def handler_{i}(request):\n    return route(request, {i})\n" for i in range(120)]
    docs = [f"# Section {i}\n\nThe handler routes an incoming request.\n" for i in range(12)]
    vs = VectorStore(safe_tmp_path / "scoped.db")
    try:
        vecs = embedder.embed(code + docs, batch_size=16)
        for i, (text, vec) in enumerate(zip(code + docs, vecs, strict=True)):
            code_chunk = i < len(code)
            lang = "python" if code_chunk else "markdown"
            vs.insert(i, f"f{i}.{'py' if code_chunk else 'md'}", 1, 3, lang, text, vec)
        vs.flush()
        hits = search("how a request is routed to a handler", embedder, vs,
                      scope="docs", top_k=_TOP_K)
    finally:
        vs.close()
    assert len(hits) == _TOP_K, (
        f"TK3: docs scope returned {len(hits)} of {_TOP_K} — filtered after the KNN, not before"
    )
    assert {h["language"] for h in hits} == {"markdown"}, (
        f"TK3: scope leaked non-docs languages: {sorted({h['language'] for h in hits})}"
    )
