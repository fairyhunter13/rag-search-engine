"""Semantic search: embed query on GPU → scope-filtered vector search → rerank."""
from __future__ import annotations

import typing
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from functools import lru_cache
from pathlib import Path

from rag_search.embed.embedder import Embedder, Reranker
from rag_search.index.discover import _TEXT_LANGS
from rag_search.index.store import VectorStore

_reranker: Reranker | None = None
_rerank_stats: dict = {"queries": 0, "top1_changed": 0}

# Candidate pool handed to the cross-encoder. The reranker moves the top result on roughly
# three queries in four, so under-feeding it is the most expensive thing this module can do;
# the old top_k*3 (=30) sits well below the 50-200 that current practice uses.
_MIN_POOL = 50

# RRF's smoothing constant. 60 is the value from the original paper and what every hybrid-search
# implementation reported through 2026 still uses; it is deliberately large relative to the ranks
# that matter, which is what stops a single lane's #1 from dominating a chunk both lanes like.
_RRF_K = 60

# Federation fan-out width. Both lanes are pure sqlite — vec0 KNN and FTS5 — so they release the
# GIL and genuinely overlap; the GPU is untouched here (one embed before the loop, one rerank
# after), so this never contends with `_GPU_INFER_LOCK`. Threads rather than processes because
# each member already holds its own connection opened `check_same_thread=False`, and no two
# workers ever touch the same one.
#
# Sized against the measurement that motivated this: inosoft-project's 157 priced members cost
# 36.68 s of sequential dense KNN (mean 233.6 ms, worst 3.28 s) against 3.00 s of lexical.
_FANOUT_WORKERS = 8


def fuse_rrf(lanes: list[list[dict]], k: int = _RRF_K) -> list[dict]:
    """Reciprocal Rank Fusion of per-lane ranked lists, best first.

    Fusing on *rank* rather than score is the whole point: cosine similarity and BM25 have no
    common scale, no common range, and opposite polarity, so there is no weighting of the two
    numbers that means anything. Rank is the one thing both lanes agree on.

    A chunk found by one lane only still scores — it simply collects one term instead of two,
    which is how a rare identifier no embedder placed near the query gets into the pool at all.
    """
    merged: dict[int, dict] = {}
    for lane in lanes:
        for rank, chunk in enumerate(lane):
            row = merged.get(chunk["chunk_id"])
            if row is None:
                row = merged[chunk["chunk_id"]] = dict(chunk) | {"fused_score": 0.0}
            else:
                row.update({j: v for j, v in chunk.items() if j not in row})
            row["fused_score"] += 1.0 / (k + rank + 1)
    return sorted(merged.values(), key=lambda c: c["fused_score"], reverse=True)


def _pool_key(chunk: dict) -> float:
    """What ranks a candidate before the cross-encoder sees it: the fused score where the lanes
    were fused, the dense score where only one lane ran."""
    return chunk.get("fused_score", chunk.get("score", 0.0))


def _get_reranker() -> Reranker:
    global _reranker
    if _reranker is None:
        _reranker = Reranker()
    return _reranker


@lru_cache(maxsize=1)
def _code_languages() -> tuple[str, ...]:
    """Every language with a tree-sitter grammar that isn't prose."""
    from tree_sitter_language_pack import SupportedLanguage
    return tuple(sorted(set(typing.get_args(SupportedLanguage)) - set(_TEXT_LANGS)))


# Every scope `scope_languages` implements, and the set the MCP + CLI docstrings must agree with.
# Declared once so a caller can *reject* an unknown scope: the function below cannot, because its
# "no restriction" answer and its "I don't know that scope" answer are the same `None`.
SCOPES = ("code", "docs", "all")


def scope_languages(scope: str) -> tuple[str, ...] | None:
    """Languages a scope admits, or None for no restriction.

    Same membership test the old post-filter applied — hoisted ahead of the KNN so the
    scope narrows the corpus instead of the result set.

    **Unknown scopes fall through to `None`, so validate against `SCOPES` before calling.** The
    fall-through widens rather than narrows — a typo'd `scope="cdoe"` searches prose and every
    other language while the caller believes it asked for code — and it is silent in the one
    direction a caller cannot detect from the results.
    """
    if scope == "docs":
        return tuple(sorted(_TEXT_LANGS))
    if scope == "code":
        return _code_languages()
    return None


def search(
    query: str,
    embedder: Embedder,
    store: VectorStore,
    *,
    scope: str = "code",
    top_k: int = 10,
) -> list[dict]:
    """Embed query on GPU, scope-filtered hybrid (dense + BM25) search, then rerank."""
    q_vec = embedder.embed([query], batch_size=1, side="query")[0].astype("float32")
    pool = max(_MIN_POOL, top_k * 5)
    langs = scope_languages(scope)
    lanes = [store.search(q_vec, top_k=pool, languages=langs),
             store.search_lexical(query, top_k=pool, languages=langs)]
    return _rank(query, fuse_rrf(lanes)[:pool], top_k)


def _rank(query: str, chunks: list[dict], top_k: int) -> list[dict]:
    """One cross-encoder pass over `chunks`, recording the rerank lift counters.

    Orders by the retrieval score first so `top1_changed` means the same thing for one store as
    for a federation, whose concatenation arrives in per-store order rather than by score.
    """
    if not chunks:
        return []
    chunks.sort(key=_pool_key, reverse=True)
    retrieval_top1 = chunks[0].get("path")
    scores = _get_reranker().rerank(query, [c.get("content", "") for c in chunks])
    for c, s in zip(chunks, scores, strict=False):
        c["rerank_score"] = s
    chunks.sort(key=lambda c: c.get("rerank_score", 0.0), reverse=True)
    _rerank_stats["queries"] += 1
    if len(chunks) >= 2 and chunks[0].get("path") != retrieval_top1:
        _rerank_stats["top1_changed"] += 1
    return _diversify(chunks, top_k)


# `chunker` emits windows with a 10-line overlap, so adjacent chunks of one file are near-duplicates
# by construction — not a ranking error to correct, but a property of how the corpus was cut.
_OVERLAP_SLACK = 10
_MAX_PER_FILE = 3


def _diversify(chunks: list[dict], top_k: int) -> list[dict]:
    """Drop chunks that overlap one already kept, and cap how many can come from one file.

    Runs **after** the cross-encoder and **before** the `top_k` cut, which is the only correct
    place: the reranker still scores every candidate in the pool, so nothing is hidden from it,
    and only the answer the caller sees is thinned. Filtering earlier would starve it.

    Both rules keep the highest-reranked member of whatever they collapse, so this can only
    reorder *within* what the reranker already preferred — it never promotes a chunk over a
    better-scoring one from a different file.

    The cap is the weaker of the two claims. Overlap collapse removes text that is literally
    duplicated; a per-file cap asserts that a searcher would rather see three files than ten
    windows of one. It shipped on the local label-free number — distinct files in the returned
    top-10 — because at the time there was no published evidence for per-file caps or MMR in code
    retrieval in either direction. There is now: arXiv:2601.23254 reports 7.04-15.58% relative EM
    over its best baseline from identifier-weighted reranking plus **structure-aware
    deduplication**. Found after the fact and it agrees; the local number is still what decides.
    """
    kept: list[dict] = []
    dropped: list[dict] = []
    per_file: dict[str, int] = {}
    for c in chunks:
        if len(kept) >= top_k:
            break
        path = c.get("path")
        if per_file.get(path, 0) >= _MAX_PER_FILE:
            dropped.append(c)
            continue
        start, end = c.get("start_line"), c.get("end_line")
        if start is not None and end is not None and any(
            k.get("path") == path and k.get("start_line") is not None
            and start <= k["end_line"] + _OVERLAP_SLACK and k["start_line"] <= end + _OVERLAP_SLACK
            for k in kept
        ):
            dropped.append(c)
            continue
        kept.append(c)
        per_file[path] = per_file.get(path, 0) + 1
    # A query whose whole pool is one file's windows must still return `top_k` results: backfill
    # in rerank order rather than hand back three because the cap was met. Diversity is a
    # preference between equally-good answers, never a reason to return fewer of them.
    if len(kept) < top_k:
        kept.extend(dropped[:top_k - len(kept)])
    return kept[:top_k]


def rerank_stats() -> dict:
    """Return a copy of the module-level rerank lift counters."""
    return dict(_rerank_stats)


def rerank_passages(query: str, passages: list[str]) -> list[float]:
    """Cross-encoder relevance scores for passages (GPU). Returns [] for empty input."""
    if not passages:
        return []
    return _get_reranker().rerank(query, passages)


def _pool_federation(
    query: str, q_vec, stores: list[VectorStore], langs, top_k: int,
) -> list[dict]:
    """Fan out over stores, fuse each member's lanes, pool and truncate. No embed, no rerank.

    Split out of `search_federation` so the order-preservation invariant can be tested through
    the code the daemon runs. `test_t4_fanout_preserves_store_order` used to reach it by
    monkeypatching `_rank` to identity, which the zero-fake guard rejects and rightly so: a
    patched ranker is a shape that never executes in production, and the seam it faked is
    exactly this function boundary. Naming the boundary is cheaper than faking it.

    Everything model-touching stays in the caller, which is what makes this callable from a test
    that must not reach the GPU.
    """
    per_store = max(top_k * 3, 10)

    def lanes(vs: VectorStore) -> list[dict]:
        return fuse_rrf([vs.search(q_vec, top_k=per_store, languages=langs),
                         vs.search_lexical(query, top_k=per_store, languages=langs)])

    chunks: list[dict] = []
    if len(stores) < 2:
        for vs in stores:
            chunks.extend(lanes(vs))
    else:
        with ThreadPoolExecutor(max_workers=min(_FANOUT_WORKERS, len(stores))) as ex:
            for part in ex.map(lanes, stores):
                chunks.extend(part)
    chunks.sort(key=_pool_key, reverse=True)
    return chunks[: max(_MIN_POOL, top_k * 5)]


def search_federation(
    query: str,
    embedder: Embedder,
    db_paths: Sequence[Path],
    *,
    scope: str = "all",
    top_k: int = 8,
    batch: int = _FANOUT_WORKERS,
) -> list[dict]:
    """Embed query ONCE, ANN-search all members, one global rerank.

    Use instead of calling search() in a loop over federation members — avoids
    N redundant GPU embeds and produces a single cross-member ranking.

    Takes *paths* and owns the store lifecycle, rather than taking stores the caller opened.
    That is a file-descriptor bound, not a style preference. A SQLite WAL connection costs three
    descriptors (db + `-wal` + `-shm`), so the previous shape — every caller opening all 157 of
    inosoft-project's priced members before calling in — held 471 of them for the length of one
    question, against systemd's default `LimitNOFILESoft` of 1024. Two federated operations
    overlapping (a `search` and an `overview(what="communities")`, which opens a GraphStore per
    member) exhausted the table, and the resulting EMFILE surfaced as `unable to open database
    file` on every project until the daemon was restarted. Opening a batch at a time bounds the
    peak at `batch * 3` regardless of how large the federation grows.

    The `ExitStack` is the other half: it closes what was opened even when a *later* open in the
    same batch raises. The caller loops this replaced built their list *outside* the `try/finally`
    that closed it, so an EMFILE partway through orphaned every handle already opened — which is
    what turned a transient shortage into a permanent wedge.

    Two-stage by construction: each store contributes a few candidates, the retrieval score
    picks the best _MIN_POOL of them fleet-wide, and only those reach the cross-encoder.
    Reranking every store's candidates instead would put thousands of passages through the
    GPU for one question — which is what the per-store loop this replaces actually did.

    Fusion is per store, then pooled. RRF scores are comparable across members by construction
    (they are functions of rank alone), where the raw lane scores are not — a store whose whole
    corpus is slightly off-topic still returns high cosines within itself.

    The fan-out (`_pool_federation`) is parallel but **order-preserving**, and that is
    load-bearing rather than incidental: `ex.map` yields in *input* order, so the pooled list is
    assembled exactly as the sequential loop assembled it, and `chunks.sort` is stable — so ties
    break identically and the ranking cannot move. Consuming completions as they land
    (`as_completed`) would reorder ties and turn a performance change into a silent retrieval
    change; `test_t4_fanout_preserves_store_order` is the gate that says so.

    Batching preserves that same invariant, and the ranking it produces is *identical* to the
    unbatched one rather than merely close. Batches are consumed in input order and each one
    pools in input order, so the concatenation is still overall member order and the stable sorts
    still break ties by pooling position. Per-batch truncation is safe for the same reason global
    truncation was: a chunk in the global top-`pool` has at most `pool - 1` chunks above it
    anywhere, so it cannot be cut by its own batch, whose competitors are a subset.
    """
    q_vec = embedder.embed([query], batch_size=1, side="query")[0].astype("float32")
    langs = scope_languages(scope)
    pool = max(_MIN_POOL, top_k * 5)
    pooled: list[dict] = []
    for i in range(0, len(db_paths), batch):
        with ExitStack() as es:
            # migrate=False: a query must never pay a store's one-time FTS backfill. Measured at
            # 11.3 s for a single 99 k-chunk member, and this loop opens every federation member.
            # Reconcile opens stores writable and so does migrate them, but it is not the
            # convergence path it looks like: measured across the hours after that shipped it
            # moved 137 owed stores to 136, because its walk is spent on real indexing work and
            # every live test run pauses it. The backfill is an explicit one-time fleet migration.
            #
            # `closing` rather than `with VectorStore(...)`: the class exposes close() and no
            # __enter__/__exit__, and a bare pair of dunders added only to satisfy this call site
            # would be more code than the stdlib adapter that already means exactly this.
            stores = [
                es.enter_context(closing(VectorStore(p, migrate=False)))
                for p in db_paths[i : i + batch]
            ]
            pooled.extend(_pool_federation(query, q_vec, stores, langs, top_k))
    pooled.sort(key=_pool_key, reverse=True)
    return _rank(query, pooled[:pool], top_k)
