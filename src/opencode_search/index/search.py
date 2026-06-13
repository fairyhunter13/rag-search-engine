"""Search handler: embed query, run VectorStore ANN, filter by scope."""
from __future__ import annotations

import numpy as np

from opencode_search.core.config import FINAL_TOP_K, project_vector_db
from opencode_search.index.store import VectorStore

_CODE_LANGS = frozenset({
    "python", "javascript", "typescript", "go", "rust", "java", "kotlin",
    "scala", "c", "cpp", "ruby", "php", "csharp", "swift", "bash", "sql",
})
_DOC_LANGS = frozenset({"markdown", "rst", "text", "html"})


def search_project(
    query: str,
    project_path: str,
    embedder,
    *,
    top_k: int = FINAL_TOP_K,
    scope: str = "code",
) -> list[dict]:
    """Embed query on GPU, ANN search the project VectorStore, return results."""
    db_path = project_vector_db(project_path)
    if not db_path.exists():
        return []
    store = VectorStore(db_path)
    try:
        q = embedder.embed([query], batch_size=1)[0].astype(np.float32)
        results = store.search(q, top_k=top_k * 2)  # oversample for filter
        if scope == "code":
            results = [r for r in results if r.get("language") in _CODE_LANGS]
        elif scope == "docs":
            results = [r for r in results if r.get("language") in _DOC_LANGS]
        return results[:top_k]
    finally:
        store.close()
