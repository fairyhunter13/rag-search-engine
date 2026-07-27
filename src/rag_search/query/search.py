"""Semantic search: embed query on GPU → scope-filtered vector search → rerank."""
from __future__ import annotations

import typing
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
    """Embed query on GPU, scope-filtered vector search, then rerank."""
    q_vec = embedder.embed([query], batch_size=1)[0].astype("float32")
    pool = max(_MIN_POOL, top_k * 5)
    return _rank(query, store.search(q_vec, top_k=pool, languages=scope_languages(scope)), top_k)


def _rank(query: str, chunks: list[dict], top_k: int) -> list[dict]:
    """One cross-encoder pass over `chunks`, recording the rerank lift counters.

    Orders by vector score first so `top1_changed` means the same thing for one store as
    for a federation, whose concatenation arrives in per-store order rather than by score.
    """
    if not chunks:
        return []
    chunks.sort(key=lambda c: c.get("score", 0.0), reverse=True)
    vector_top1 = chunks[0].get("path")
    scores = _get_reranker().rerank(query, [c.get("content", "") for c in chunks])
    for c, s in zip(chunks, scores, strict=False):
        c["rerank_score"] = s
    chunks.sort(key=lambda c: c.get("rerank_score", 0.0), reverse=True)
    _rerank_stats["queries"] += 1
    if len(chunks) >= 2 and chunks[0].get("path") != vector_top1:
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

    Two-stage by construction: each store contributes a few candidates, the vector score
    picks the best _MIN_POOL of them fleet-wide, and only those reach the cross-encoder.
    Reranking every store's candidates instead would put thousands of passages through the
    GPU for one question — which is what the per-store loop this replaces actually did.
    """
    q_vec = embedder.embed([query], batch_size=1)[0].astype("float32")
    langs = scope_languages(scope)
    per_store = max(top_k * 3, 10)
    chunks: list[dict] = []
    for vs in stores:
        chunks.extend(vs.search(q_vec, top_k=per_store, languages=langs))
    chunks.sort(key=lambda c: c.get("score", 0.0), reverse=True)
    return _rank(query, chunks[: max(_MIN_POOL, top_k * 5)], top_k)
