"""Semantic search: embed query on GPU → vector search → scope-filtered results → rerank."""
from __future__ import annotations

from opencode_search.embed.embedder import Embedder, Reranker
from opencode_search.index.discover import _CODE_LANGS, _TEXT_LANGS
from opencode_search.index.store import VectorStore

_reranker: Reranker | None = None


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
        allowed = _CODE_LANGS if scope == "code" else (_TEXT_LANGS if scope == "docs" else frozenset())
        results = [r for r in results if r.get("language") in allowed]
    passages = [r.get("content", "") for r in results]
    scores = _get_reranker().rerank(query, passages)
    for r, s in zip(results, scores, strict=False):
        r["rerank_score"] = s
    results.sort(key=lambda r: r.get("rerank_score", 0.0), reverse=True)
    return results[:top_k]
