"""Ask handler: context assembly from vector chunks + community context (no LLM)."""
from __future__ import annotations

_MAX_CTX = 3000

# The two assemblies `ask` actually has. Five scope names stood here; `wiki` left with tier 3, and
# `global`/`feature`/`business` were names for orderings of the same two ingredients. Every scope
# runs the identical chunk search — see run_ask's one _search_fed call, which takes no scope — so a
# scope only ever chose how the community map was selected and which half came first. `business`
# was distinguished by a semantic_type filter and `feature` by the unranked selector; both writers
# left with the narrator, and what remained were aliases advertised as capabilities.
_SCOPES = ("all", "architecture")


def _scope_error(scope: str) -> str:
    return f"unknown scope={scope!r} — valid: {', '.join(_SCOPES)}"


def _community_summaries(query: str, stores: list, top_k: int = 8) -> str:
    """Top-k L1 community summaries across federated stores, ranked by cross-encoder.

    Three near-duplicate selectors stood here: two reranked ones differing only in candidate
    limit, and an unranked third that fed `feature`/`business` the first N rows by id. Since
    `label_community_structural` writes every summary from one template, "first N by id" meant
    the context was whichever communities happened to be inserted first. Ranking is the only
    thing that makes a templated summary worth reading, so both surviving scopes now share it.

    `narrated=1` used to gate this pool. Its one writer was the LLM narrator deleted with tier 3,
    so the filter would now select nothing; the cross-encoder discriminates instead, and it scores
    filler low without being told to.
    """
    from rag_search.query.search import rerank_passages

    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for store in stores:
        for ctitle, csumm in store._con.execute(
            "SELECT title,summary FROM communities WHERE level=1 "
            "AND summary IS NOT NULL AND summary!='' AND kind NOT IN ('dir','file') "
            "ORDER BY id LIMIT 50"
        ).fetchall():
            if ctitle and ctitle not in seen:
                seen.add(ctitle)
                candidates.append((ctitle, csumm))
    if not candidates:
        return ""
    scores = rerank_passages(query, [c[1] for c in candidates])
    ranked = sorted(zip(scores, candidates, strict=False), key=lambda x: x[0], reverse=True)
    return "\n\n".join(f"[{c}]\n{s}" for _, (c, s) in ranked[:top_k])


def _assemble_context(query: str, chunks: list[dict], stores: list, scope: str) -> str:
    """Assemble pre-built context string from DB artifacts — no LLM call."""
    if scope not in _SCOPES:
        # An unrecognized scope used to fall through to plain chunk context, which reads as a
        # successful answer to a caller who mistyped one — the failure mode `overview` already
        # avoids by returning its valid set (server/_overview.py).
        return _scope_error(scope)
    chunk_ctx = "\n\n".join(
        f"[{r.get('path', '')}:{r.get('start_line', '')}]\n{r.get('content', '')}"
        for r in chunks
    )[:_MAX_CTX]
    community_ctx = _community_summaries(query, stores)[:_MAX_CTX]
    if scope == "architecture":
        return f"## Architecture\n{community_ctx}\n\n## Code\n{chunk_ctx}"
    return f"## Code\n{chunk_ctx}\n\n## Architecture\n{community_ctx}"


def compose_answer(query: str, chunks: list[dict], stores: list, *, scope: str = "all") -> str:
    """Return pre-built context assembled from DB artifacts — NO LLM generation.

    stores: list of open GraphStore objects (root first; federation members included).
    Used by the MCP ask handler (read-only path).
    """
    return _assemble_context(query, chunks, stores, scope)


def run_ask(query: str, project_path: str = "", scope: str = "all") -> str:
    """Assemble context from DB artifacts (no LLM). Shared by MCP + CLI."""
    from rag_search.core.config import index_dir, project_graph_db, project_vector_db
    from rag_search.core.registry import list_projects
    from rag_search.embed.embedder import get_embedder
    from rag_search.graph.store import GraphStore
    from rag_search.index.store import VectorStore
    from rag_search.query.answer_cache import get as _cache_get
    from rag_search.query.answer_cache import set as _cache_set
    from rag_search.query.search import search_federation as _search_fed
    # Checked before the embed so a mistyped scope costs no GPU work, not only before assembly.
    if scope not in _SCOPES:
        return _scope_error(scope)
    if project_path:
        from rag_search.core.registry import resolve_registered_root
        project_path = resolve_registered_root(project_path)
    _auto_selected = not project_path
    if not project_path:
        projects = [p for p in list_projects() if p.enabled]
        if not projects:
            return "No indexed projects found."
        if len(projects) > 1:
            _cands = "\n".join(f"  - {p.path}" for p in projects[:12])
            return ("project_path required — multiple projects are indexed and none could be "
                    f"inferred. Pass one of:\n{_cands}")
        project_path = projects[0].path
    # Disclose the fallback so a caller who omitted project_path can't mistake this
    # answer for a different, intended project (no equivalent of search()'s projects_searched here).
    _prefix = (
        f"[project auto-selected: {project_path} — pass project_path to target a specific project]\n\n"
        if _auto_selected else ""
    )
    cache_dir = index_dir(project_path) / "ask_cache"
    cache_key = f"{scope}:{query}"
    cached = _cache_get(cache_dir, cache_key)
    if cached:
        return _prefix + cached
    from rag_search.daemon.federation import expand_federation
    all_paths = expand_federation(project_path)
    if not project_vector_db(project_path).exists():
        return f"Project not indexed: {project_path}"
    embedder = get_embedder()
    graph_stores = [GraphStore(project_graph_db(p)) for p in all_paths if project_graph_db(p).exists()]
    vector_stores = [VectorStore(project_vector_db(p)) for p in all_paths if project_vector_db(p).exists()]
    try:
        chunks = _search_fed(query, embedder, vector_stores, top_k=8)
        answer = compose_answer(query, chunks, graph_stores, scope=scope)
        _cache_set(cache_dir, cache_key, answer, ttl_s=3600)
        return _prefix + answer
    finally:
        for vs in vector_stores:
            vs.close()
        for gs in graph_stores:
            gs.close()
