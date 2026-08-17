"""TK1-TK3, TK10-TK15: gates for the token-efficiency passes (plan Phase 1).

Each fails on the code that preceded it, which is what makes it a gate rather than decoration:
TK1 on a tool that had no verbosity knob and always returned bodies, TK2 on the per-project
loop that embedded the query once per federation member, TK3 on the scope post-filter that
returned a remnant of the pool instead of a full one; TK10-TK15 on the four uncapped envelopes
of the August pass. The gap in the middle is not an omission: the `TK` namespace is per-repo, not
per-file, and the ids it skips are held by `test_hybrid_retrieval`, `test_gpu_autodetect` and
`test_model_governance`.
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


# A federated call over this fleet echoes 137 member paths at ~85 bytes each. The envelope that
# carries eight hits must not be dominated by the list of places they were not found.
_ENVELOPE_BYTES_MAX = 1024


def _fake_members(n: int) -> list[str]:
    return [f"/w/git/github.com/org/service-{i:03d}" for i in range(n)]


def _fake_hits(members: list[str]) -> list[dict]:
    return [
        {"path": f"{m}/src/app/handler.py", "start_line": 10, "end_line": 42,
         "language": "python", "content": "def handler():\n    return 1\n", "score": 1.0}
        for m in members
    ]


def test_tk10_a_large_federation_does_not_echo_its_member_list():
    """TK10: above the echo threshold the envelope reports counts, not the whole member array.

    The discriminator is the pairing, not the ceiling on its own: a small federation must still
    get its `projects_searched` list, because for a scoped call that array is short and it is the
    only thing that says what the scope actually resolved to. A shaping change that dropped the
    array unconditionally would satisfy a byte ceiling and destroy that.

    The pre-Phase-1 envelope is rebuilt here rather than described, so the assertion compares
    against the real old shape and not against a number someone wrote down once.
    """
    from rag_search.server.mcp import _SEARCHED_ECHO_MAX, _search_payload

    members = _fake_members(137)
    hits = _fake_hits(members[:3])
    old = json.dumps({"results": hits, "total": len(hits), "elapsed_ms": 12,
                      "projects_searched": members})
    new = json.dumps(_search_payload(hits, members, len(members), 12, "compact"))

    assert len(new) < len(old) // 2, (
        f"TK10: envelope is {len(new)} bytes against the old {len(old)} — the member array is "
        "still dominating the payload"
    )
    assert len(new) < _ENVELOPE_BYTES_MAX, (
        f"TK10: envelope is {len(new)} bytes, over the {_ENVELOPE_BYTES_MAX} ceiling"
    )
    body = json.loads(new)
    assert "projects_searched" not in body, "TK10: the full member array survived the threshold"
    assert body["projects_searched_count"] == 137
    assert body["projects_with_hits"] == members[:3], (
        "TK10: the caller can no longer tell which projects answered, which is the one thing the "
        "member array was carrying that mattered"
    )

    small = _fake_members(_SEARCHED_ECHO_MAX)
    kept = json.loads(json.dumps(
        _search_payload(_fake_hits(small[:1]), small, len(small), 5, "compact")
    ))
    assert kept["projects_searched"] == small, (
        "TK10: a scoped call lost its member list — the threshold is meant to leave it alone"
    )


def test_tk11_hits_are_credited_to_the_member_not_its_root():
    """TK11: attribution takes the longest matching root.

    A federation member lives inside its root and both are in `projects_searched`, so a
    first-match walk credits every member's hit to the root and reports one project answering
    when forty did. The bug is invisible to a byte ceiling and to any assertion that only counts.
    """
    from rag_search.server.mcp import _projects_with_hits

    root = "/w/ws"
    member = "/w/ws/repositories/domain-ledger"
    hits = [{"path": f"{member}/Services/AdjustmentService.php"},
            {"path": f"{root}/docs/README.md"}]
    assert _projects_with_hits(hits, [root, member]) == [member, root], (
        "TK11: a member's hit was credited to its enclosing root"
    )


def test_tk14_the_shaped_envelope_is_reachable_through_the_real_surface(federation_root_path):
    """TK14: TK10 shapes a dict; this asserts the daemon's own search actually returns that shape.

    AU5/AU6 are the precedent and the reason: AU5 passed for five days against a hand-built dict
    while the payload that reached callers had no such block in it. A pure-function gate with no
    reachability companion measures a function nobody calls.
    """
    from rag_search.server.mcp import _SEARCHED_ECHO_MAX, _search_sync

    payload = json.loads(
        _search_sync("checkout total", "all", [federation_root_path], _TOP_K, "compact")
    )
    if "error" in payload:
        raise AssertionError(f"TK14: search returned an error, not a payload: {payload['error']}")
    members = payload.get("projects_searched_count", len(payload.get("projects_searched", [])))
    if members <= _SEARCHED_ECHO_MAX:
        assert "projects_searched" in payload, (
            f"TK14: {members} members is at or under the threshold, so the array must be kept"
        )
        return
    assert "projects_searched" not in payload, (
        f"TK14: {members} members and the full array still reached the caller"
    )
    assert "projects_with_hits" in payload and "projects_skipped_count" in payload, (
        f"TK14: the replacement keys never reached the surface: {sorted(payload)}"
    )


def test_tk12_status_reports_the_members_a_caller_can_act_on():
    """TK12: above the threshold `members` becomes the flagged subset plus counts.

    The pairing is the discriminator again: a healthy member must be *counted* and not listed,
    and an unhealthy one must survive. Dropping the array outright passes a byte ceiling and
    removes the only rows the call exists to surface.
    """
    from rag_search.server._overview import _MEMBERS_ECHO_MAX, _members_block

    def _m(i, **over):
        row = {"path": f"/w/ws/repositories/svc-{i:03d}", "index_state": "ready",
               "symbols": 900, "communities": 12, "edges": 400, "symbol_hollow": False,
               "hierarchy_quality": {"degenerate": False}}
        row.update(over)
        return row

    healthy = [_m(i) for i in range(137)]
    sick = [_m(900, index_state="indexing"), _m(901, symbol_hollow=True),
            _m(902, hierarchy_quality={"degenerate": True})]
    members = healthy + sick

    block = _members_block(members)
    assert "members" not in block, "TK12: the full member array survived the threshold"
    assert block["member_count"] == 140
    assert block["members_healthy_count"] == 137
    assert [m["path"] for m in block["members_needing_attention"]] == [m["path"] for m in sick], (
        "TK12: the actionable rows are not the ones that came back"
    )
    assert len(json.dumps(block)) < len(json.dumps({"members": members})) // 8, (
        "TK12: the block is still dominated by members reporting that they are fine"
    )

    small = [_m(i) for i in range(_MEMBERS_ECHO_MAX)]
    assert _members_block(small) == {"members": small}, (
        "TK12: a small federation lost its member list — the threshold must leave it alone"
    )


def test_tk13_the_language_rung_tail_is_capped_without_moving_a_total():
    """TK13: capping the emitted rows must not change one number summed from them.

    The trap is capping `rows` before the rollup, which silently rewrites `dark_files` and
    `coverage_pct` — the numbers the whole extraction apparatus exists to drive down. So this
    asserts the totals against an uncapped computation, not against a constant.
    """
    from rag_search.server._overview import _LANG_RUNG_MAX, _extraction_totals

    rows = [{"language": f"lang{i}", "rung": "generic" if i % 2 else "treesitter",
             "files": 100 - i, "symbols": i, "files_with_symbols": i,
             "anon": 0, "errors": 0}
            for i in range(_LANG_RUNG_MAX + 30)]
    tot = _extraction_totals(rows)

    assert len(tot["by_language_rung"]) == _LANG_RUNG_MAX, "TK13: the tail was not capped"
    assert tot["by_language_rung_truncated"] == 30
    assert tot["files"] == sum(r["files"] for r in rows), (
        "TK13: `files` was summed from the capped list — the cap moved a headline total"
    )
    assert tot["dark_files"] == sum(r["files"] - r["files_with_symbols"] for r in rows)
    assert sum(r["files"] for r in tot["by_rung"]) == tot["files"], (
        "TK13: `by_rung` no longer accounts for every file"
    )
    assert [r["files"] for r in tot["by_language_rung"]] == \
        sorted((r["files"] for r in rows), reverse=True)[:_LANG_RUNG_MAX], (
        "TK13: the rows kept are not the largest ones"
    )


def test_tk15_unfiltered_projects_returns_counts_and_query_still_finds_a_row():
    """TK15: the 236-row dump becomes counts, and the existing `query` knob recovers any row."""
    from rag_search.server._overview import _projects_block

    class _P:
        def __init__(self, path, federation=None):
            self.path, self.enabled, self.indexed_at = path, True, "2026-08-17T00:00:00Z"
            self.last_change_seen, self.federation = 1_700_000_000, federation or []

    rows = [_P(f"/w/git/github.com/org/service-{i:03d}") for i in range(236)]
    rows[0].federation = [p.path for p in rows[1:138]]

    bare = _projects_block(rows, "")
    assert "projects" not in bare, "TK15: the full project list survived an unfiltered call"
    assert bare["project_count"] == 236
    assert bare["federation_members_count"] == 137
    assert len(json.dumps(bare)) < len(json.dumps({"projects": [
        {"path": p.path, "enabled": p.enabled, "indexed_at": p.indexed_at,
         "last_change_seen": p.last_change_seen} for p in rows]})) // 4

    found = _projects_block(rows, "service-119")
    assert [r["path"] for r in found["projects"]] == ["/w/git/github.com/org/service-119"], (
        "TK15: the filter cannot recover a row the unfiltered call no longer lists"
    )
    assert found["total"] == 236
