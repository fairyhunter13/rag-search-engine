"""Semantic search: embed query on GPU → vector search → scope-filtered results."""
from __future__ import annotations

from opencode_search.embed.embedder import Embedder
from opencode_search.index.store import VectorStore

_SCOPE_LANGS: dict[str, set[str]] = {
    "docs": {"markdown", "rst", "text", "html"},
    "code": {
        "python", "javascript", "typescript", "go", "rust", "java",
        "kotlin", "ruby", "php", "csharp", "swift", "bash", "sql", "cpp", "c",
    },
}


def search(
    query: str,
    embedder: Embedder,
    store: VectorStore,
    *,
    scope: str = "code",
    top_k: int = 10,
) -> list[dict]:
    """Embed query on GPU, search vector store, filter by scope.

    Caller is responsible for opening and closing the store.
    """
    q_vec = embedder.embed([query], batch_size=1)[0].astype("float32")
    results = store.search(q_vec, top_k=top_k * 3)
    if scope != "all":
        allowed = _SCOPE_LANGS.get(scope, set())
        results = [r for r in results if r.get("language") in allowed]
    return results[:top_k]
