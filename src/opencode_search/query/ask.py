"""Ask handler: context assembly from vector chunks + community context (no LLM)."""
from __future__ import annotations

_MAX_CTX = 3000


def _top_communities_semantic(query: str, stores: list, top_k: int = 10) -> str:
    """Select top-k communities by cross-encoder reranking across all federated stores (GPU)."""
    from opencode_search.query.search import rerank_passages

    seen: set = set()
    rows: list = []
    for store in stores:
        for r in store._con.execute(
            "SELECT title, summary FROM communities "
            "WHERE summary IS NOT NULL AND summary != '' "
            "AND narrated=1 "
            "AND (semantic_type IS NULL OR semantic_type NOT IN ('test')) "
            "AND kind NOT IN ('dir','file') "
            "ORDER BY level, id LIMIT 50"
        ).fetchall():
            if r[0] not in seen:
                seen.add(r[0])
                rows.append(r)
    if not rows:
        return ""
    scores = rerank_passages(query, [r[1] for r in rows])
    ranked = sorted(zip(scores, rows, strict=False), key=lambda x: x[0], reverse=True)
    return "\n\n".join(f"## {r[0]}\n{r[1]}" for _, r in ranked[:top_k] if r[1])


def _macro_community_context(stores: list, limit: int = 5) -> str:
    """Fetch L2+ domain summaries across all federated stores."""
    seen: set = set()
    rows: list = []
    for store in stores:
        for r in store._con.execute(
            "SELECT title, summary FROM communities WHERE level>=2 "
            "AND summary IS NOT NULL AND summary!='' "
            "AND narrated=1 "
            "AND (semantic_type IS NULL OR semantic_type NOT IN ('test')) "
            "AND kind NOT IN ('dir','file') "
            "ORDER BY member_count DESC LIMIT ?", (limit,)
        ).fetchall():
            if r[0] not in seen:
                seen.add(r[0])
                rows.append(r)
    return "\n\n".join(f"## Domain: {r[0]}\n{r[1]}" for r in rows[:limit] if r[1])


def _community_context(stores: list, limit: int = 20, semantic_types: tuple[str, ...] = ()) -> str:
    seen: set = set()
    rows: list = []
    for store in stores:
        if semantic_types:
            placeholders = ",".join("?" * len(semantic_types))
            src = store._con.execute(
                f"SELECT title, summary FROM communities WHERE summary IS NOT NULL AND summary != '' "
                f"AND narrated=1 AND semantic_type IN ({placeholders}) ORDER BY level, id LIMIT ?",
                (*semantic_types, limit),
            ).fetchall()
        else:
            src = store._con.execute(
                "SELECT title, summary FROM communities "
                "WHERE summary IS NOT NULL AND summary != '' "
                "AND narrated=1 "
                "AND (semantic_type IS NULL OR semantic_type NOT IN ('test')) "
                "AND kind NOT IN ('dir','file') "
                "ORDER BY level, id LIMIT ?", (limit,),
            ).fetchall()
        for r in src:
            if r[0] not in seen:
                seen.add(r[0])
                rows.append(r)
    return "\n\n".join(f"## {r[0]}\n{r[1]}" for r in rows[:limit] if r[1])


def _tree_walk_context(query: str, stores: list, top_k: int = 8) -> str:
    """Hierarchy tree-walk: rank L1 communities within L2 domain context (Phase 2.5).

    Drills parent_id (L2 domains → L1 children) then reranks by summary relevance.
    Returns traceable context ([Domain → Community] pairs). Falls back to flat L1 pool
    when no L2 hierarchy exists yet.
    """
    from opencode_search.query.search import rerank_passages
    candidates: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for store in stores:
        dom = store._con.execute(
            "SELECT id, COALESCE(title,'Domain') FROM communities WHERE level>=2 ORDER BY member_count DESC LIMIT 10"
        ).fetchall()
        if dom:
            for did, dtitle in dom:
                for ctitle, csumm in store._con.execute(
                    "SELECT title,summary FROM communities WHERE parent_id=? AND narrated=1 AND summary IS NOT NULL AND summary!='' AND kind NOT IN ('dir','file') LIMIT 15", (did,)
                ).fetchall():
                    if ctitle and ctitle not in seen:
                        seen.add(ctitle);candidates.append((dtitle,ctitle,csumm))
        else:
            for r in store._con.execute(
                "SELECT title,summary FROM communities WHERE level=1 AND narrated=1 AND summary IS NOT NULL AND summary!='' AND kind NOT IN ('dir','file') LIMIT 20"
            ).fetchall():
                if r[0] and r[0] not in seen:
                    seen.add(r[0]);candidates.append(("",r[0],r[1]))
    if not candidates:
        return ""
    scores = rerank_passages(query, [c[2] for c in candidates])
    ranked = sorted(zip(scores,candidates,strict=False),key=lambda x:x[0],reverse=True)
    return "\n\n".join((f"[{d} → {c}]" if d else f"[{c}]")+"\n"+s for _,(d,c,s) in ranked[:top_k])


def _assemble_context(query: str, chunks: list[dict], stores: list, scope: str) -> str:
    """Assemble pre-built context string from DB artifacts — no LLM call."""
    chunk_ctx = "\n\n".join(
        f"[{r.get('path', '')}:{r.get('start_line', '')}]\n{r.get('content', '')}"
        for r in chunks
    )[:_MAX_CTX]
    if scope == "global":
        tree_ctx = _tree_walk_context(query, stores)[:_MAX_CTX]
        macro = _macro_community_context(stores)
        community_ctx = (f"{macro}\n\n{tree_ctx}" if macro else tree_ctx)[:_MAX_CTX]
        return f"## Architecture (hierarchy tree-walk)\n{community_ctx}\n\n## Code\n{chunk_ctx}"
    if scope in ("architecture", "all"):
        tree_ctx = _tree_walk_context(query, stores)[:_MAX_CTX]
        macro = _macro_community_context(stores)
        community_ctx = (f"{macro}\n\n{tree_ctx}" if macro else tree_ctx)[:_MAX_CTX]
        return f"## Code\n{chunk_ctx}\n\n## Architecture (tree-walk)\n{community_ctx}"
    if scope == "wiki":
        community_ctx = _community_context(stores, limit=10)[:_MAX_CTX]
        return f"## Wiki\n{community_ctx}\n\n## Code\n{chunk_ctx}"
    if scope == "feature":
        return (
            f"## Code (feature trace)\n{chunk_ctx}\n\n"
            f"## Community context\n{_community_context(stores, limit=5)[:_MAX_CTX]}"
        )
    if scope == "business":
        biz_ctx = _community_context(stores, limit=20,
                                     semantic_types=("business_rule", "business_process", "feature"))[:_MAX_CTX]
        return f"## Business context\n{biz_ctx}\n\n## Code\n{chunk_ctx}"
    return chunk_ctx


def compose_answer(query: str, chunks: list[dict], stores: list, *, scope: str = "all") -> str:
    """Return pre-built context assembled from DB artifacts — NO LLM generation.

    stores: list of open GraphStore objects (root first; federation members included).
    Used by the MCP ask handler (read-only path).
    """
    return _assemble_context(query, chunks, stores, scope)
