"""Ask handler: multi-scope LLM synthesis from vector chunks + community context."""
from __future__ import annotations

from opencode_search.graph.llm import chat
from opencode_search.graph.store import GraphStore

_MAX_CTX = 3000


def _top_communities_semantic(query: str, store: GraphStore, top_k: int = 10) -> str:
    """Select top-k communities by cosine similarity to the query (GPU embed)."""
    import numpy as np

    from opencode_search.embed.embedder import get_embedder

    rows = store._con.execute(
        "SELECT title, summary FROM communities "
        "WHERE summary IS NOT NULL AND summary != '' ORDER BY level, id LIMIT 50",
    ).fetchall()
    if not rows:
        return ""
    embedder = get_embedder()
    q_vec = embedder.embed([query])[0].astype(np.float32)
    s_vecs = embedder.embed([r[1] for r in rows]).astype(np.float32)
    scores = s_vecs @ q_vec
    top = np.argsort(scores)[::-1][:top_k]
    return "\n\n".join(f"## {rows[i][0]}\n{rows[i][1]}" for i in top if rows[i][1])


def _community_context(store: GraphStore, limit: int = 20, semantic_types: tuple[str, ...] = ()) -> str:
    if semantic_types:
        placeholders = ",".join("?" * len(semantic_types))
        rows = store._con.execute(
            f"SELECT title, summary FROM communities "
            f"WHERE summary IS NOT NULL AND summary != '' "
            f"AND semantic_type IN ({placeholders}) ORDER BY level, id LIMIT ?",
            (*semantic_types, limit),
        ).fetchall()
    else:
        rows = store._con.execute(
            "SELECT title, summary FROM communities "
            "WHERE summary IS NOT NULL AND summary != '' ORDER BY level, id LIMIT ?",
            (limit,),
        ).fetchall()
    return "\n\n".join(f"## {r[0]}\n{r[1]}" for r in rows if r[1])


def ask(
    query: str,
    chunks: list[dict],
    store: GraphStore,
    *,
    scope: str = "all",
) -> str:
    """Synthesize an answer using chunk excerpts and community summaries.

    chunks: results from query.search.search()
    store: open GraphStore for community context
    scope: all | architecture | global | feature | wiki | business
    """
    chunk_ctx = "\n\n".join(
        f"[{r.get('path', '')}:{r.get('start_line', '')}]\n{r.get('content', '')}"
        for r in chunks
    )[:_MAX_CTX]

    if scope == "global":
        community_ctx = _top_communities_semantic(query, store)[:_MAX_CTX]
        context = f"## Architecture (MAP+REDUCE)\n{community_ctx}\n\n## Code\n{chunk_ctx}"
    elif scope in ("architecture", "all"):
        community_ctx = _community_context(store)[:_MAX_CTX]
        context = f"## Code\n{chunk_ctx}\n\n## Architecture\n{community_ctx}"
    elif scope == "wiki":
        community_ctx = _community_context(store, limit=10)[:_MAX_CTX]
        context = f"## Wiki\n{community_ctx}\n\n## Code\n{chunk_ctx}"
    elif scope == "feature":
        context = (
            f"## Code (feature trace)\n{chunk_ctx}\n\n"
            f"## Community context\n{_community_context(store, limit=5)[:_MAX_CTX]}"
        )
    elif scope == "business":
        biz_ctx = _community_context(store, limit=20,
                                     semantic_types=("rule", "constraint", "feature", "workflow", "process"))[:_MAX_CTX]
        context = f"## Business context\n{biz_ctx}\n\n## Code\n{chunk_ctx}"
    else:
        context = chunk_ctx

    return chat(
        f"You are a code intelligence assistant. Answer concisely.\n\n"
        f"Question: {query}\n\nContext:\n{context}\n\nAnswer:"
    )
