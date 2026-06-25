"""Semantic search: embed query on GPU → vector search → scope-filtered results → rerank."""
from __future__ import annotations

from opencode_search.embed.embedder import Embedder, Reranker
from opencode_search.index.discover import _TEXT_LANGS
from opencode_search.index.store import VectorStore

_reranker: Reranker | None = None
_rerank_stats: dict = {"queries": 0, "top1_changed": 0}


def _get_reranker() -> Reranker:
    global _reranker
    if _reranker is None:
        _reranker = Reranker()
    return _reranker


def search(
    query: str,
    embedder: Embedder,
    store: VectorStore,
    *,
    scope: str = "code",
    top_k: int = 10,
) -> list[dict]:
    """Embed query on GPU, search vector store, filter by scope, then rerank."""
    if scope == "similar":
        scope = "all"
    q_vec = embedder.embed([query], batch_size=1)[0].astype("float32")
    results = store.search(q_vec, top_k=top_k * 3)
    if scope != "all":
        if scope == "docs":
            results = [r for r in results if r.get("language") in _TEXT_LANGS]
        elif scope == "code":
            from tree_sitter_language_pack import has_language
            results = [
                r for r in results
                if has_language(r.get("language", "")) and r.get("language") not in _TEXT_LANGS
            ]
    passages = [r.get("content", "") for r in results]
    scores = _get_reranker().rerank(query, passages)
    for r, s in zip(results, scores, strict=False):
        r["rerank_score"] = s
    vector_top1 = results[0].get("path") if results else None
    results.sort(key=lambda r: r.get("rerank_score", 0.0), reverse=True)
    _rerank_stats["queries"] += 1
    if len(results) >= 2 and results[0].get("path") != vector_top1:
        _rerank_stats["top1_changed"] += 1
    return results[:top_k]


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
    top_k: int = 8,
) -> list[dict]:
    """Embed query ONCE, ANN-search all stores, one global rerank.

    Use instead of calling search() in a loop over federation members — avoids
    N redundant GPU embeds and produces a single cross-member ranking.
    """
    q_vec = embedder.embed([query], batch_size=1)[0].astype("float32")
    chunks: list[dict] = []
    for vs in stores:
        chunks.extend(vs.search(q_vec, top_k=top_k * 3))
    if not chunks:
        return []
    passages = [c.get("content", "") for c in chunks]
    scores = _get_reranker().rerank(query, passages)
    for c, s in zip(chunks, scores, strict=False):
        c["rerank_score"] = s
    chunks.sort(key=lambda c: c.get("rerank_score", 0.0), reverse=True)
    return chunks[:top_k]
