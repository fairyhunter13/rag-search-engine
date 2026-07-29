"""TK4 + HY1/HY2 + LX1/LX1b/LX2 — gates for the lexical lane and its fusion with dense retrieval.

The LX gates cover the two defects found by tracing the fleet after 2d shipped: the one-time FTS
backfill was running inside `_open` on the *query* path (11.31 s for one 99 k-chunk member, once
per federation member), and `search_lexical` joined `chunks` inside the MATCH query, so sqlite
hydrated every matched row before sorting. Both were invisible in output — correct answers,
paid for at hundreds of times the necessary cost.

TK4 is the plan's own gate for Phase 2 and is red by construction before it: dense retrieval
alone cannot rank a chunk highly for a name the embedder has never seen.

HY1 exists because the failure mode of an external-content FTS5 index is silent. Nothing errors
when it drifts from `chunks`; it just answers with rows that are no longer there, or stops
answering for rows that are. Only an explicit consistency check sees that.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live

# A name no embedder has been trained on, in a chunk whose prose is deliberately ordinary. The
# distractors below are *closer* to the query in meaning; the target's only advantage is that it
# spells the identifier. That is exactly the shape the dense lane cannot exploit.
_RARE = "qzrmvltx_ledger_reconcile"


def _reconcile_store(embedder, path):
    """One target chunk naming `_RARE`, buried in chunks that are semantically nearer the query."""
    from rag_search.index.store import VectorStore

    distractors = [
        f"def reconcile_ledger_{i}(entries):\n"
        f"    # reconcile the ledger, matching entries against statements\n"
        f"    return balance(entries, tolerance={i})\n"
        for i in range(120)
    ]
    # A call site, not the definition: the chunk is overwhelmingly about PDF layout, so its
    # pooled embedding sits nowhere near the query and the identifier is its only link to it.
    # This is the everyday case the dense lane is worst at — "where is this symbol used".
    target = (
        "class InvoicePrinter:\n"
        "    def render(self, page):\n"
        "        # fonts, margins, page breaks, then assemble the PDF content stream\n"
        "        self._stream.write(self._fonts.embed(page.margins, page.breaks))\n"
        f"        return {_RARE}(page.rows)\n"
    )
    texts = [*distractors, target]
    vs = VectorStore(path)
    vecs = embedder.embed(texts, batch_size=16)
    for i, (text, vec) in enumerate(zip(texts, vecs, strict=True)):
        vs.insert(i, f"f{i}.py", 1, 3, "python", text, vec)
    vs.flush()
    return vs, len(texts) - 1


def test_tk4_lexical_lane_finds_what_dense_misses(embedder, safe_tmp_path):
    """TK4: a rare literal identifier dense ranks outside the top 10 must land top-3 after RRF.

    The dense-only measurement is not context — it is the half of the gate that stops this
    passing vacuously. Without it, a query the embedder happened to get right would report the
    lexical lane working while it did nothing at all ([[feedback_guard_tests_must_discriminate]]).
    """
    from rag_search.query.search import search

    vs, target_id = _reconcile_store(embedder, safe_tmp_path / "tk4.db")
    try:
        q_vec = embedder.embed([_RARE], batch_size=1)[0].astype("float32")
        dense = vs.search(q_vec, top_k=10)
        assert all(h["chunk_id"] != target_id for h in dense), (
            f"TK4 is vacuous: dense retrieval already ranks {_RARE} in its top 10, so the fused "
            "result below would pass with the lexical lane contributing nothing"
        )
        hits = search(_RARE, embedder, vs, scope="code", top_k=10)
    finally:
        vs.close()
    ranks = [i for i, h in enumerate(hits) if h["chunk_id"] == target_id]
    assert ranks and ranks[0] < 3, (
        f"TK4: {_RARE} lands at rank {ranks[0] if ranks else 'absent'} of "
        f"{[h['path'] for h in hits]} — the lexical lane is not reaching the fused pool"
    )


def test_hy1_lexical_index_tracks_the_chunks_table(embedder, safe_tmp_path):
    """HY1: every write path leaves the FTS index describing exactly what `chunks` holds.

    Drives the three sites that own `chunks` — insert, delete_by_path, clear — and checks both
    directions each time: FTS5's own integrity-check catches an index claiming rows that are
    gone, and re-querying catches one that stopped covering rows that are still there.
    """
    vs, _ = _reconcile_store(embedder, safe_tmp_path / "hy1.db")
    try:
        def check(where: str) -> None:
            vs._con.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')")
            got = {h["chunk_id"] for h in vs.search_lexical("statements", top_k=500)}
            live = {r[0] for r in vs._con.execute(
                "SELECT chunk_id FROM chunks WHERE content LIKE '%statements%'")}
            assert got == live, (
                f"HY1 after {where}: lexical index returns {sorted(got - live)} that are not in "
                f"chunks, and misses {sorted(live - got)} that are"
            )

        vec = embedder.embed(["def unrelated():\n    return None\n"], batch_size=1)[0]
        check("insert")
        vs.delete_by_path("f0.py")
        vs.flush()
        check("delete_by_path")
        # The watcher's own cycle, and the one that makes an orphaned index row *observable*:
        # `_chunk_id` hashes path and position, so re-indexing a purged file lands its chunks on
        # exactly the rowids the purge freed. A row the purge failed to remove then answers
        # queries for text the file no longer contains, under a chunk_id that resolves again.
        vs.delete_by_path("f1.py")
        vs.insert(1, "f1.py", 1, 3, "python", "def unrelated():\n    return None\n", vec)
        vs.flush()
        check("re-index of a purged path")
        # Overwriting a chunk_id that is still live: the other way the same orphan gets made.
        vs.insert(2, "f2.py", 1, 3, "python", "def also_unrelated():\n    return None\n", vec)
        vs.flush()
        check("insert over an existing chunk_id")
        vs.clear()
        vs.flush()
        assert vs.search_lexical("statements", top_k=500) == [], (
            "HY1: clear() emptied chunks but left the lexical index populated"
        )
    finally:
        vs.close()


def _unmigrate(vs) -> None:
    """Put a store back into the shape every pre-2d index has on disk: rows in `chunks`, an empty
    FTS index, no `fts_rev` stamp. This is the state 137 of the fleet's 172 stores were in when
    the lexical lane shipped, not a contrived one.
    """
    vs._con.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('delete-all')")
    vs._con.execute("DELETE FROM meta WHERE key='fts_rev'")
    vs._con.commit()


def test_lx1_backfill_never_runs_on_the_query_path(embedder, safe_tmp_path):
    """LX1: opening a store to *read* must not migrate it, and must degrade instead of stalling.

    Measured on the live fleet: opening `gims` (99 k chunks) inside a query cost 11.31 s to then
    answer in 1.9 ms, and a federated search opens one store per member — so the first query
    against the 189-member federation would have paid that per member, serially, and timed out.
    The `fts_rev` assertion is the discriminating half: returning no lexical hits is also what a
    migrated-but-empty store does, so only "the stamp is still absent" proves the backfill was
    skipped rather than run.
    """
    from rag_search.index.store import VectorStore

    path = safe_tmp_path / "lx1.db"
    vs, _ = _reconcile_store(embedder, path)
    _unmigrate(vs)
    vs.close()
    ro = VectorStore(path, migrate=False)
    try:
        assert ro.lexical_ready is False
        assert ro.search_lexical("statements", top_k=10) == []
        assert ro.get_meta("fts_rev") is None, (
            "LX1: a read-only open built the FTS index — the 11 s backfill is back on the query "
            "path, where a federated search pays it once per member"
        )
        q_vec = embedder.embed(["reconcile the ledger"], batch_size=1)[0].astype("float32")
        assert ro.search(q_vec, top_k=5), "LX1: dense retrieval must still answer"
    finally:
        ro.close()
    rw = VectorStore(path, migrate=True)
    try:
        assert rw.lexical_ready is True
        assert rw.get_meta("fts_rev") is not None
        assert rw.search_lexical("statements", top_k=10), (
            "LX1: the writable open did not build the index the reading open refused to build"
        )
    finally:
        rw.close()


def test_lx1b_writes_through_an_unmigrated_store_stay_repairable(embedder, safe_tmp_path):
    """LX1b: skipping FTS maintenance on an unbuilt index must leave it *rebuildable*, not wrong.

    This is why the write sites skip rather than raise. An external-content index cannot be
    maintained before it exists — `delete` against a row it never saw writes a negative entry —
    so a store that takes writes while unmigrated must still come out correct the first time
    something rebuilds it. 137 fleet stores were taking watcher writes in exactly this state.
    """
    from rag_search.index.store import VectorStore

    path = safe_tmp_path / "lx1b.db"
    vs, _ = _reconcile_store(embedder, path)
    _unmigrate(vs)
    vs.close()
    vec = embedder.embed(["def unrelated():\n    return None\n"], batch_size=1)[0]
    un = VectorStore(path, migrate=False)
    try:
        un.delete_by_path("f0.py")
        un.insert(1, "f1.py", 1, 3, "python", "ledger statements reconciled here", vec)
        un.flush()
    finally:
        un.close()
    vs2 = VectorStore(path, migrate=True)
    try:
        vs2._con.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')")
        got = {h["chunk_id"] for h in vs2.search_lexical("statements", top_k=500)}
        live = {r[0] for r in vs2._con.execute(
            "SELECT chunk_id FROM chunks WHERE content LIKE '%statements%'")}
        assert got == live, (
            f"LX1b: after writes through an unmigrated handle the rebuilt index returns "
            f"{sorted(got - live)} that are gone and misses {sorted(live - got)} that are not"
        )
    finally:
        vs2.close()


# The known-bad shape, as a control. Retyped deliberately: its whole job is to be the query the
# production one is no longer, so it must not track production.
_NAIVE_SQL = """
SELECT c.chunk_id, c.content, bm25(chunks_fts)
FROM chunks_fts JOIN chunks c ON c.chunk_id = chunks_fts.rowid
WHERE chunks_fts MATCH 'ledger' ORDER BY bm25(chunks_fts) LIMIT 5
"""


def _hydrates_after_ranking(con, sql: str) -> tuple[bool, list[str]]:
    """True when sqlite sorts before it joins `chunks` — i.e. hydrates top_k, not every match."""
    plan = [r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql)]
    hydrate = next(i for i, d in enumerate(plan) if "SEARCH c" in d)
    return any("ORDER BY" in d for d in plan[:hydrate]), plan


def test_lx2_lexical_query_ranks_before_it_hydrates(embedder, safe_tmp_path):
    """LX2: `chunks` is joined only to the rows that survived the LIMIT.

    Joining inside the MATCH query makes sqlite hydrate every matched row before sorting, so a
    three-common-word question over a 207 k-chunk store read 178 k full chunk bodies to return
    ten (323 ms; 130 ms once split). Asserted on the plan sqlite chose for the statement the
    method *actually ran* — read back off a trace callback rather than retyped here, so it
    cannot drift from production — and not on elapsed time, which is noise on a shared GPU box,
    nor on VM steps, which miss it: the cost is bytes of `content`, not instructions (measured
    1.1x by step count against 2.5x by clock).
    """
    vs, _ = _reconcile_store(embedder, safe_tmp_path / "lx2.db")
    try:
        seen: list[str] = []
        vs._con.set_trace_callback(seen.append)
        hits = vs.search_lexical("reconcile the ledger statements", top_k=5)
        vs._con.set_trace_callback(None)
        assert hits, "LX2 is vacuous: the query matched nothing to rank"
        control, cplan = _hydrates_after_ranking(vs._con, _NAIVE_SQL)
        assert not control, (
            f"LX2 is vacuous: the naive join already ranks before it hydrates on this sqlite, "
            f"so the assertion below would pass without the split. Plan was {cplan}"
        )
        live, plan = _hydrates_after_ranking(
            vs._con, next(s for s in seen if "chunks_fts" in s and "MATCH" in s))
        assert live, (
            f"LX2: `chunks` is joined before the ranking sort, so every matched row is hydrated "
            f"to return top_k. Plan was {plan}"
        )
    finally:
        vs.close()


_ROWID_IN_SQL = """
SELECT chunks_fts.rowid, bm25(chunks_fts) AS rk
FROM chunks_fts
WHERE chunks_fts MATCH 'ledger'
  AND chunks_fts.rowid IN (SELECT chunk_id FROM chunks WHERE language IN ('python'))
ORDER BY rk LIMIT 5
"""


def _fts_scan_step(con, sql: str) -> tuple[str, list[str]]:
    """The plan line for the FTS5 table itself, plus the whole plan for the failure message."""
    plan = [r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql)]
    return next(d for d in plan if "VIRTUAL TABLE INDEX" in d), plan


def test_lx4_scope_filter_never_becomes_an_fts_rowid_constraint(embedder, safe_tmp_path):
    """LX4: a language scope must not be pushed into the MATCH as a rowid set.

    `AND chunks_fts.rowid IN (SELECT chunk_id FROM chunks WHERE language IN (...))` reads like
    the cheap pre-filter and is the single most expensive line this engine has had. sqlite treats
    a rowid set as a constraint FTS5 can serve, so the plan flips from `VIRTUAL TABLE INDEX 0:M1`
    to `0:=M1` and the full-text query is re-run once per candidate rowid. Measured on a
    118 k-chunk member: 0.018 s unfiltered, 17.0 s filtered. `scope="code"` names 302 languages,
    so the candidate set is nearly the whole corpus, and across inosoft-project's 157 federation
    members that shape cost 700 s of a pinned core for one search — past the 300 s the MCP client
    waits, which is why every federated search "timed out". Joining `chunks` in phase 1 instead
    keeps the FTS index driving and probes by integer primary key per match: 0.049 s, same rows.

    Asserted on the plan for the statement `search_lexical` *actually ran*, read off a trace
    callback rather than retyped here, so it cannot drift from production — the same construction
    LX2 uses, and for the same reason. Elapsed time is not the assertion: the cost only shows at
    a corpus size no hermetic fixture has, while the plan flip is visible at any size.
    """
    vs, _ = _reconcile_store(embedder, safe_tmp_path / "lx4.db")
    try:
        control, cplan = _fts_scan_step(vs._con, _ROWID_IN_SQL)
        assert control.endswith(":=M1"), (
            f"LX4 is vacuous: the rowid IN-list does not become an FTS5 equality constraint on "
            f"this sqlite, so the assertion below would pass without the join. Plan was {cplan}"
        )
        seen: list[str] = []
        vs._con.set_trace_callback(seen.append)
        hits = vs.search_lexical("reconcile the ledger statements", top_k=5,
                                 languages=("python", "javascript"))
        vs._con.set_trace_callback(None)
        assert hits, "LX4 is vacuous: the scoped query matched nothing"
        assert all(h["language"] == "python" for h in hits), (
            "LX4: the filter admitted a language it was not given — phase 1 must pre-filter"
        )
        live, plan = _fts_scan_step(
            vs._con, next(s for s in seen if "chunks_fts" in s and "MATCH" in s))
        assert not live.endswith(":=M1"), (
            f"LX4: the language scope reached FTS5 as a rowid constraint, so the match re-runs "
            f"per candidate row. Filter by joining `chunks` in phase 1 instead. Plan was {plan}"
        )
    finally:
        vs.close()


@pytest.mark.parametrize("raw", [
    'worker-side validation (async)', 'a OR b NOT c', 'foo* "bar" col:val', '-- ***', '',
])
def test_hy2_operator_characters_cannot_reach_fts5(embedder, safe_tmp_path, raw):
    """HY2: FTS5's query language must never see raw user text.

    `-`, `*`, `:`, `"`, `(` and the bare words AND/OR/NOT are all operators, so an unsanitised
    question is a syntax error rather than a query — and a caught-and-ignored one would turn
    into a silently dense-only search that still looks like it works.
    """
    from rag_search.index.store import VectorStore

    vs = VectorStore(safe_tmp_path / "hy2.db")
    try:
        vs.insert(1, "a.py", 1, 2, "python", "async worker side validation of a bar value",
                  embedder.embed(["x"], batch_size=1)[0])
        vs.flush()
        vs.search_lexical(raw, top_k=5)  # must not raise
    finally:
        vs.close()


def test_lx3_write_cache_is_on_the_write_path_only(safe_tmp_path):
    """LX3: the 128 MB page cache belongs to write-path handles and must never reach a query one.

    Both halves fail to opposite defects. Dropping the pragma costs 1.94x on indexing statement
    time (5.46-5.77s to 2.85-2.94s over 50k rows) and is invisible in every output — the class of
    regression that needs an assertion on configuration rather than on results. Applying it
    unconditionally is the worse direction and equally invisible: a federated search opens one
    handle per member, and 189 members at 128 MB is a 24 GB ask to answer one question.

    The query-path expectation is read off a bare connection rather than written down as -2000, so
    a build with a different compiled default does not turn this into a false alarm.
    """
    import sqlite3

    from rag_search.index.store import VectorStore

    bare = sqlite3.connect(":memory:")
    default = bare.execute("PRAGMA cache_size").fetchone()[0]
    bare.close()

    path = safe_tmp_path / "lx3.db"
    rw = VectorStore(path, migrate=True)
    try:
        assert rw._con.execute("PRAGMA cache_size").fetchone()[0] == -131072, (
            "LX3: the write path lost its page cache — indexing pays 1.94x in statement time, "
            "with nothing wrong in any result to notice it by"
        )
    finally:
        rw.close()
    ro = VectorStore(path, migrate=False)
    try:
        assert ro._con.execute("PRAGMA cache_size").fetchone()[0] == default, (
            "LX3: a query-path handle claimed the 128 MB write cache — one handle is opened per "
            "federation member, so this is a 24 GB allocation on the largest federation here"
        )
    finally:
        ro.close()
