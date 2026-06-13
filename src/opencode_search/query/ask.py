"""Ask handler: multi-scope LLM synthesis from vector chunks + community context."""
from __future__ import annotations

from opencode_search.graph.llm import chat
from opencode_search.graph.store import GraphStore

_MAX_CTX = 3000


def _community_context(store: GraphStore, limit: int = 20) -> str:
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

    if scope in ("global", "architecture", "all"):
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
    else:
        context = chunk_ctx

    return chat(
        f"You are a code intelligence assistant. Answer concisely.\n\n"
        f"Question: {query}\n\nContext:\n{context}\n\nAnswer:"
    )
