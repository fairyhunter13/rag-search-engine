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

# Eight hits carry at most 8 x 200 = 1,600 chars of preview; the rest is paths and five short
# fields. 4096 leaves roughly 2.5x headroom for that and still fails hard the moment bodies come
# back, since the fleet's mean chunk is 838 chars and eight of those alone are ~6.7 KB.
_COMPACT_BYTES_MAX = 4096


def test_tk1_compact_response_fits_a_byte_budget(sample_workspace):
    """TK1: compact must be small in absolute terms, and small *because it dropped bodies*.

    A size ratio is the tempting assertion and the wrong one: these fixture projects are 1-4 KB
    files, so their chunks are short and correct code can land near any fixed ratio by accident.
    What cannot happen by accident is compact carrying no body while full carries one — so that
    pairing is the discriminator, and the byte ceiling is the regression guard.
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
    assert len(compact) < len(full), f"TK1: compact {len(compact)}B >= full {len(full)}B"
    assert all("content" not in h for h in hits), "TK1: compact results still carry chunk bodies"
    assert all("content" in h for h in json.loads(full)["results"]), (
        "TK1: verbosity='full' stopped returning bodies — compact is now the only mode, so the "
        "size comparison above proves nothing"
    )
    assert all(h["path"] and h["start_line"] for h in hits), (
        "TK1: compact dropped bodies without leaving a location to Read"
    )


def test_tk2_one_embed_for_a_whole_federation(federation_root_path):
    """TK2: a federated search embeds the query once, not once per member.

    A latency assertion would not discriminate here — N embeds of one short string are fast
    enough to hide the defect — so this counts the real calls the GPU actually served.

    Counted per *thread*, which is the correction of 2026-07-31. The process-wide `embed_stats`
    this used to read is moved by every embed anywhere in the interpreter, and this suite runs
    two fleet-scale embedders in-process on background threads: `mcp.index()` hands
    `reconcile_projects` to a daemon thread that walks the whole registry, and the watcher's
    dispatch workers reach `_index_project` on theirs. Neither is joined to the test that started
    it. Instrumented over a full fast run, one leaked `Thread-N (reconcile_projects)` embedded 21
    times spread across later, unrelated tests; CI run 30611283020 failed here with "2 query
    embeds for 4 members" for exactly that reason, and the same run passed in isolation.

    Attribution rather than a retry loop or a widened bound: the property under test is what one
    call site spent, so the honest fix is to measure that and not to tolerate a second embed —
    which is the defect itself. The leak it exposed is real and separate; the daemon has the same
    background threads and it is only the suite that runs them beside a measurement.
    """
    from rag_search.embed.embedder import embed_calls_here
    from rag_search.server.mcp import _search_sync

    before = embed_calls_here()
    payload = json.loads(
        _search_sync("checkout total", "all", [federation_root_path], _TOP_K, "compact")
    )
    spent = embed_calls_here() - before
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
