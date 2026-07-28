"""TK4 + HY1/HY2 — gates for the lexical lane and its fusion with dense retrieval.

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
