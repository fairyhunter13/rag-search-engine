"""BQ1-BQ7 — gates for the coarse bit lane that fronts the float32 KNN, and for the write path
that has to keep it and `vec_chunks` reachable from `chunks` (BQ7).

Its failure mode is silent, exactly like the external-content FTS index HY1 guards: nothing
errors when `vec_chunks_bin` drifts from `vec_chunks`. A stale code keeps a deleted chunk in
every shortlist, and a missing one hides a live chunk from every query — both answer, neither
complains. Only an explicit consistency check sees it, so BQ1 asserts identity of the two id
sets *and* that each code still equals the quantization of its own vector.

BQ2 pins the thing that would otherwise shift silently: the two-stage returns the same score
scale as the exact scan (1.0 - L2), not merely a correlated ranking, because search.py fuses
those numbers with bm25 and a rescaled dense score would reweight hybrid retrieval.
"""
from __future__ import annotations

import numpy as np
import pytest

pytestmark = pytest.mark.live

_TEXTS = [
    f"def handler_{i}(request):\n    # route {i}\n    return dispatch(request, {i})\n"
    for i in range(60)
]


def _exact(path):
    """A second handle on the same store, configured above the size line so `search` takes the
    float32 lane. Every corpus here is a few hundred chunks — far below BIN_MIN_CHUNKS, where the
    bit lane costs more than it saves — so the lane under test is asked for by configuration."""
    from rag_search.index.store import VectorStore

    return VectorStore(path, migrate=False, min_two_stage=10**9)


def _store(embedder, path, texts=None, langs=None, **kw):
    from rag_search.index.store import VectorStore

    texts = texts or _TEXTS
    langs = langs or ["python" if i % 2 == 0 else "markdown" for i in range(len(texts))]
    vs = VectorStore(path, **kw)
    vecs = embedder.embed(texts, batch_size=16)
    for i, (text, vec, lang) in enumerate(zip(texts, vecs, langs, strict=True)):
        vs.insert(i, f"f{i}.py", 1, 3, lang, text, vec)
    vs.flush()
    return vs, vecs


def _assert_bin_consistent(vs, where: str) -> int:
    """Every vector has exactly one code, and every code is that vector's own quantization."""
    vec_ids = {r[0] for r in vs._con.execute("SELECT chunk_id FROM vec_chunks")}
    bin_ids = {r[0] for r in vs._con.execute("SELECT chunk_id FROM vec_chunks_bin")}
    assert vec_ids == bin_ids, (
        f"{where}: bit index drifted — {len(vec_ids - bin_ids)} vector(s) with no code, "
        f"{len(bin_ids - vec_ids)} code(s) with no vector"
    )
    stale = vs._con.execute("""
        SELECT COUNT(*) FROM vec_chunks v JOIN vec_chunks_bin b USING (chunk_id)
        WHERE b.code != vec_quantize_binary(v.embedding)
    """).fetchone()[0]
    assert stale == 0, f"{where}: {stale} code(s) no longer match their vector"
    return len(vec_ids)


def test_bq1_bit_index_survives_insert_replace_delete_and_clear(embedder, safe_tmp_path):
    """BQ1: the four write sites keep the bit index identical to vec_chunks, or searches lie."""
    vs, vecs = _store(embedder, safe_tmp_path / "bq1.db")
    assert vs.bin_ready, "bit lane was not built for a freshly indexed store"
    assert _assert_bin_consistent(vs, "after insert") == len(_TEXTS)

    # Replace a live chunk_id in place. vec0 has no upsert, so `insert` deletes first; if it
    # forgets the bin row the code keeps describing the *previous* chunk's vector forever.
    vs.insert(0, "f0.py", 1, 3, "python", "def replaced():\n    return 1\n", vecs[-1])
    vs.flush()
    _assert_bin_consistent(vs, "after replacing a chunk_id")

    vs.delete_by_path("f5.py")
    vs.flush()
    assert _assert_bin_consistent(vs, "after delete_by_path") == len(_TEXTS) - 1

    vs.clear()
    vs.flush()
    assert _assert_bin_consistent(vs, "after clear") == 0
    vs.close()


def test_bq2_two_stage_scores_match_the_exact_scan(embedder, safe_tmp_path):
    """BQ2: same score scale as the float32 KNN, and a stored vector finds itself first."""
    path = safe_tmp_path / "bq2.db"
    vs, vecs = _store(embedder, path, min_two_stage=0)
    q = np.asarray(vecs[7], dtype=np.float32)

    two = vs.search(q, top_k=5)
    exact = _exact(path).search(q, top_k=5)   # the path this must not diverge from

    assert two[0]["chunk_id"] == exact[0]["chunk_id"] == 7, "query vector did not find its own chunk"
    by_id = {h["chunk_id"]: h["score"] for h in exact}
    shared = [h for h in two if h["chunk_id"] in by_id]
    assert len(shared) >= 4, "two-stage shortlist lost most of the exact top-5"
    for h in shared:
        assert abs(h["score"] - by_id[h["chunk_id"]]) < 1e-5, (
            f"score scale diverged for chunk {h['chunk_id']}: "
            f"{h['score']} vs exact {by_id[h['chunk_id']]}"
        )
    vs.close()


_BQ3_CODE = [f"def route_{i}(req):\n    return dispatch(req, {i})\n" for i in range(200)]
_BQ3_DOCS = [f"# Release notes {i}\n\nWhat shipped in cut {i} of the docs site.\n" for i in range(15)]


def test_bq3_language_filter_returns_a_full_page(embedder, safe_tmp_path):
    """BQ3: the filter runs inside the KNN, not over its output.

    Sized so the plausible wrong version goes red: 200 python chunks, 15 markdown, a python
    query, and a 40-candidate shortlist. Post-filtering that shortlist keeps only the markdown
    that reached the *global* top 40 — a silently short page. A half-and-half 60-chunk corpus
    passes both ways, which is how this assertion first read as a gate while being decoration.
    """
    from rag_search.index.store import BIN_OVERSAMPLE

    texts = _BQ3_CODE + _BQ3_DOCS
    langs = ["python"] * len(_BQ3_CODE) + ["markdown"] * len(_BQ3_DOCS)
    vs, vecs = _store(embedder, safe_tmp_path / "bq3.db", texts, langs, min_two_stage=0)
    assert 10 <= len(_BQ3_DOCS) < 10 * BIN_OVERSAMPLE < len(texts), "corpus cannot show the bug"
    q = np.asarray(vecs[0], dtype=np.float32)
    hits = vs.search(q, top_k=10, languages=["markdown"])
    assert len(hits) == 10, f"asked for 10 markdown chunks, got {len(hits)}"
    assert {h["language"] for h in hits} == {"markdown"}
    assert vs.search(q, top_k=5, languages=[]) == []
    vs.close()


def _recall_at_k(exact, approx, queries, k=10) -> float:
    """Share of the exact scan's top-k that a bit-lane handle recovers, averaged over queries."""
    got = 0
    for q in queries:
        truth = {h["chunk_id"] for h in exact.search(q, k)}
        got += len(truth & {h["chunk_id"] for h in approx.search(q, k)})
    return got / (len(queries) * k)


def test_bq5_oversample_earns_its_value(embedder, safe_tmp_path):
    """BQ5: gate BIN_OVERSAMPLE against the exact scan it approximates.

    The end-to-end recall gate cannot do this job: over its 40 golden queries, oversample 1 and a
    *disabled* rescore score identically to the shipped config (gnDCG differs by 3e-4) because a
    cross-encoder re-sorts a 10-deep page and recall@10 is saturated. Hence a gap assertion, not
    just a floor: a floor alone passes on a broken shortlist that a small corpus flatters.
    """
    from rag_search.index.store import BIN_OVERSAMPLE, VectorStore

    texts = [f"{v} {n}(self, {a}):\n    return self.{a}.{v}({n}_id)\n"
             for v in ("fetch", "delete", "render", "validate", "publish", "resolve", "queue")
             for n in ("invoice", "session", "webhook", "manifest", "checkout", "ledger")
             for a in ("client", "cache", "broker", "store", "index", "pool", "shard")]
    over = BIN_OVERSAMPLE
    assert len(texts) > 10 * over, "shortlist must be smaller than the corpus to measure anything"
    path = safe_tmp_path / "bq5.db"
    vs, vecs = _store(embedder, path, texts, ["python"] * len(texts), min_two_stage=0)
    queries = [np.asarray(v, dtype=np.float32) for v in vecs[::6]]

    exact = _exact(path)
    shipped = _recall_at_k(exact, vs, queries)
    narrow = _recall_at_k(
        exact, VectorStore(path, migrate=False, oversample=1, min_two_stage=0), queries)
    vs.close()
    assert shipped >= 0.90, f"oversample {over} recovers only {shipped:.3f} of the exact top-10"
    assert shipped - narrow >= 0.05, (
        f"widening bought nothing: {shipped:.3f} at {over}x vs {narrow:.3f} at 1x — either the "
        "rescore is not running or this corpus is too small to tell the two apart")


def test_bq6_small_store_answers_on_the_exact_lane(embedder, safe_tmp_path):
    """BQ6: under BIN_MIN_CHUNKS the two-stage must not run — below the crossover it only costs.

    Which lane ran is invisible in a result (both score the same vectors identically), so the
    codes are emptied while `bin_rev` stays stamped: the bit lane would then shortlist nothing and
    answer [], while the exact scan is unaffected. Asserted in both directions, because a
    predicate wired to a constant rather than to this store's size satisfies the first half alone.
    """
    from rag_search.index.store import BIN_MIN_CHUNKS, VectorStore

    path = safe_tmp_path / "bq6.db"
    vs, vecs = _store(embedder, path)
    assert vs.bin_ready and vs.count() == len(_TEXTS) < BIN_MIN_CHUNKS, "not below the line"
    vs._con.execute("DELETE FROM vec_chunks_bin")
    vs.flush()
    q = np.asarray(vecs[3], dtype=np.float32)
    assert vs.search(q, top_k=5)[0]["chunk_id"] == 3, "small store did not take the exact lane"
    assert VectorStore(path, migrate=False, min_two_stage=0).search(q, top_k=5) == [], (
        "the lane choice ignores store size — the bit lane ran against an emptied index")
    vs.close()


def test_bq4_unquantized_store_still_answers(embedder, safe_tmp_path):
    """BQ4: a store whose codes were never built falls back to the exact scan, not to nothing.

    The dangerous direction is silence: `search` must not consult an empty bit index and report
    no matches. Reproduces the real case of a store only ever opened on the query path.
    """
    from rag_search.index.store import VectorStore

    path = safe_tmp_path / "bq4.db"
    vs, vecs = _store(embedder, path)
    vs.close()
    reopened = VectorStore(path)
    reopened._con.execute("DELETE FROM meta WHERE key='bin_rev'")
    reopened._con.execute("DELETE FROM vec_chunks_bin")
    reopened.flush()
    reopened.close()

    query = VectorStore(path, migrate=False)
    assert not query.bin_ready, "an unbackfilled store must not claim a usable bit lane"
    hits = query.search(np.asarray(vecs[3], dtype=np.float32), top_k=5)
    assert hits and hits[0]["chunk_id"] == 3, "fallback to the exact scan did not answer"
    query.close()


def test_bq7_a_vector_orphaned_from_chunks_does_not_wedge_the_store(embedder, safe_tmp_path):
    """BQ7: `insert` heals a vec row whose `chunks` row is gone, instead of raising forever.

    Not hypothetical. Measured 2026-08-04 during the nomic migration: one fleet store held 33
    rows in `vec_chunks` against 28 in `chunks`, and every re-embed of it died on
    `UNIQUE constraint failed on vec_chunks primary key` — reconcile logged a warning, walked
    on, and left that store alone on the previous embedder while the other 207 converged.

    The cause was that both the FTS delete and the vec0 deletes were gated on a probe of
    `chunks`, which made `chunks` the authority on what `vec_chunks` contains. Only the FTS
    delete needs that (it needs the text that was indexed); vec0 needs the key alone. The
    orphan is unreachable by `delete_by_path` too, since that also enumerates from `chunks`,
    so nothing short of `clear()` could ever remove it.

    The orphan is manufactured the same way the real one must have arisen — a `chunks` row
    removed with its vector left behind — because there is no supported call that produces it.
    """
    path = safe_tmp_path / "bq7.db"
    vs, vecs = _store(embedder, path)

    vs._con.execute("DELETE FROM chunks WHERE chunk_id=?", (11,))
    vs.flush()
    assert vs._con.execute(
        "SELECT COUNT(*) FROM vec_chunks WHERE chunk_id=11"
    ).fetchone()[0] == 1, "the orphan this test exists to heal was not created"

    # The re-embed that used to abort: same chunk_id, new vector, no `chunks` row to probe.
    vs.insert(11, "f11.py", 1, 3, "python", _TEXTS[11], vecs[11])
    vs.flush()

    assert vs._con.execute(
        "SELECT COUNT(*) FROM vec_chunks WHERE chunk_id=11"
    ).fetchone()[0] == 1, "the orphan was left beside the new vector rather than replaced"
    _assert_bin_consistent(vs, "after healing an orphaned vector")
    hits = vs.search(np.asarray(vecs[11], dtype=np.float32), top_k=3)
    assert hits and hits[0]["chunk_id"] == 11, "the healed chunk is not searchable"
    vs.close()


def test_bq8_a_stranded_vector_is_both_named_and_removable(embedder, safe_tmp_path):
    """BQ8: the state the validator calls INVALID has a repair, and a count that cannot hide it.

    BQ7 heals a stranded row only when the same `chunk_id` is re-inserted, and on a chunk that
    no longer exists in the source it never will be. Four fleet stores carried 3-8 such rows on
    2026-08-05 and were repaired by hand with a direct DELETE, because the engine had no call
    that could reach them.

    The second half is the counting. `orphan_count` was `abs(chunks - vec_chunks)`, so this
    store — one vector with no chunk, one chunk with no vector — read as **zero orphans** on the
    one check whose verdict is INVALID. The two faults are manufactured together for exactly
    that reason: either alone passes the old arithmetic too.
    """
    from rag_search.index.validate import vector_row_health

    path = safe_tmp_path / "bq8.db"
    vs, vecs = _store(embedder, path)

    vs._con.execute("DELETE FROM chunks WHERE chunk_id=?", (11,))  # a vector with no chunk
    vs._con.execute(  # and a chunk with no vector, to make the totals equal again
        "INSERT INTO chunks (chunk_id, path, start_line, end_line, language, content, tokens)"
        " VALUES (?,?,?,?,?,?,?)", (9001, "f9001.py", 1, 3, "python", "def unembedded(): ...", ""),
    )
    vs.flush()
    counts = vs._con.execute(
        "SELECT (SELECT COUNT(*) FROM chunks), (SELECT COUNT(*) FROM vec_chunks)"
    ).fetchone()
    assert counts[0] == counts[1], "the cancellation this test is about was not set up"

    health = vector_row_health(vs._con)
    assert health == {"stranded_vectors": 1, "missing_vectors": 1, "orphan_count": 2}, (
        f"the two faults cancelled instead of being counted: {health}")
    assert vs.orphan_vector_ids() == [11], (
        f"the stranded row was not named: {vs.orphan_vector_ids()}")

    assert vs.prune_orphan_vectors() == 1, "the stranded row was not removed"
    assert vs.prune_orphan_vectors() == 0, "prune is not idempotent"
    after = vector_row_health(vs._con)
    assert after["stranded_vectors"] == 0, "the vector outlived its prune"
    assert after["missing_vectors"] == 1, (
        "prune deleted a live chunk's row — it must only ever remove vectors `chunks` has forgotten")

    _assert_bin_consistent(vs, "after pruning a stranded vector")
    hits = vs.search(np.asarray(vecs[12], dtype=np.float32), top_k=3)
    assert hits and hits[0]["chunk_id"] == 12, "the store stopped searching after the repair"
    vs.close()
