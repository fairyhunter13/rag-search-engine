"""Semantic search: embed query on GPU → scope-filtered vector search → rerank."""
from __future__ import annotations

import typing
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

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
# Sized against the measurement that motivated this: redacted-name-10-project's 157 priced members cost
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


def scope_languages(scope: str) -> tuple[str, ...] | None:
    """Languages a scope admits, or None for no restriction.

    Same membership test the old post-filter applied — hoisted ahead of the KNN so the
    scope narrows the corpus instead of the result set.
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
    q_vec = embedder.embed([query], batch_size=1)[0].astype("float32")
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
    return chunks[:top_k]


def rerank_stats() -> dict:
    """Return a copy of the module-level rerank lift counters."""
    return dict(_rerank_stats)


def rerank_passages(query: str, passages: list[str]) -> list[float]:
    """Cross-encoder relevance scores for passages (GPU). Returns [] for empty input."""
    if not passages:
        return []
    return _get_reranker().rerank(query, passages)


def search_federation(
    query: str,
    embedder: Embedder,
    stores: list[VectorStore],
    *,
    scope: str = "all",
    top_k: int = 8,
) -> list[dict]:
    """Embed query ONCE, ANN-search all stores, one global rerank.

    Use instead of calling search() in a loop over federation members — avoids
    N redundant GPU embeds and produces a single cross-member ranking.

    Two-stage by construction: each store contributes a few candidates, the retrieval score
    picks the best _MIN_POOL of them fleet-wide, and only those reach the cross-encoder.
    Reranking every store's candidates instead would put thousands of passages through the
    GPU for one question — which is what the per-store loop this replaces actually did.

    Fusion is per store, then pooled. RRF scores are comparable across members by construction
    (they are functions of rank alone), where the raw lane scores are not — a store whose whole
    corpus is slightly off-topic still returns high cosines within itself.

    The fan-out is parallel but **order-preserving**, and that is load-bearing rather than
    incidental: `ex.map` yields in *input* order, so the pooled list is assembled exactly as the
    sequential loop assembled it, and `chunks.sort` is stable — so ties break identically and the
    ranking cannot move. Consuming completions as they land (`as_completed`) would reorder ties
    and turn a performance change into a silent retrieval change; `test_t4_fanout_preserves_store_order`
    is the gate that says so.
    """
    q_vec = embedder.embed([query], batch_size=1)[0].astype("float32")
    langs = scope_languages(scope)
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
    return _rank(query, chunks[: max(_MIN_POOL, top_k * 5)], top_k)
