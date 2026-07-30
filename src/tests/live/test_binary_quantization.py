"""BQ1-BQ4 — gates for the coarse bit lane that fronts the float32 KNN.

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


def _store(embedder, path, texts=None, langs=None):
    from rag_search.index.store import VectorStore

    texts = texts or _TEXTS
    langs = langs or ["python" if i % 2 == 0 else "markdown" for i in range(len(texts))]
    vs = VectorStore(path)
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
    vs, vecs = _store(embedder, safe_tmp_path / "bq2.db")
    q = np.asarray(vecs[7], dtype=np.float32)

    two = vs.search(q, top_k=5)
    vs._bin_ready = False          # the exact path this must not diverge from
    exact = vs.search(q, top_k=5)
    vs._bin_ready = True

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
    vs, vecs = _store(embedder, safe_tmp_path / "bq3.db", texts, langs)
    assert 10 <= len(_BQ3_DOCS) < 10 * BIN_OVERSAMPLE < len(texts), "corpus cannot show the bug"
    q = np.asarray(vecs[0], dtype=np.float32)
    hits = vs.search(q, top_k=10, languages=["markdown"])
    assert len(hits) == 10, f"asked for 10 markdown chunks, got {len(hits)}"
    assert {h["language"] for h in hits} == {"markdown"}
    assert vs.search(q, top_k=5, languages=[]) == []
    vs.close()


def _recall_at_k(vs, queries, k=10) -> float:
    """Share of the exact scan's top-k that the two-stage recovers, averaged over queries."""
    got = 0
    for q in queries:
        vs._bin_ready = False
        truth = {h["chunk_id"] for h in vs.search(q, k)}
        vs._bin_ready = True
        got += len(truth & {h["chunk_id"] for h in vs.search(q, k)})
    return got / (len(queries) * k)


def test_bq5_oversample_earns_its_value(embedder, safe_tmp_path, monkeypatch):
    """BQ5: gate BIN_OVERSAMPLE against the exact scan it approximates.

    The end-to-end recall gate cannot do this job: over its 40 golden queries, oversample 1 and a
    *disabled* rescore score identically to the shipped config (gnDCG differs by 3e-4) because a
    cross-encoder re-sorts a 10-deep page and recall@10 is saturated. Hence a gap assertion, not
    just a floor: a floor alone passes on a broken shortlist that a small corpus flatters.
    """
    import rag_search.index.store as mod

    texts = [f"{v} {n}(self, {a}):\n    return self.{a}.{v}({n}_id)\n"
             for v in ("fetch", "delete", "render", "validate", "publish", "resolve", "queue")
             for n in ("invoice", "session", "webhook", "manifest", "checkout", "ledger")
             for a in ("client", "cache", "broker", "store", "index", "pool", "shard")]
    over = mod.BIN_OVERSAMPLE
    assert len(texts) > 10 * over, "shortlist must be smaller than the corpus to measure anything"
    vs, vecs = _store(embedder, safe_tmp_path / "bq5.db", texts, ["python"] * len(texts))
    queries = [np.asarray(v, dtype=np.float32) for v in vecs[::6]]

    shipped = _recall_at_k(vs, queries)
    monkeypatch.setattr(mod, "BIN_OVERSAMPLE", 1)
    narrow = _recall_at_k(vs, queries)
    vs.close()
    assert shipped >= 0.90, f"oversample {over} recovers only {shipped:.3f} of the exact top-10"
    assert shipped - narrow >= 0.05, (
        f"widening bought nothing: {shipped:.3f} at {over}x vs {narrow:.3f} at 1x — either the "
        "rescore is not running or this corpus is too small to tell the two apart")


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
