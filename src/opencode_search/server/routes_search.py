"""Search, ask, patterns, and classify HTTP routes."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from opencode_search.core.config import FINAL_TOP_K, project_graph_db, project_vector_db


async def _api_search_get(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    query = request.query_params.get("query", "")
    top_k = int(request.query_params.get("top_k", str(FINAL_TOP_K)))
    if not query:
        return JSONResponse({"error": "query required"}, status_code=400)
    from opencode_search.core.registry import list_projects
    from opencode_search.embed.embedder import Embedder
    from opencode_search.index.store import VectorStore
    from opencode_search.query.search import search
    embedder = Embedder()
    embedder.warmup()
    if project:
        from opencode_search.daemon.federation import expand_federation
        paths = expand_federation(project)
    else:
        paths = [p.path for p in list_projects() if p.enabled]
    results = []
    for path in paths:
        vdb = project_vector_db(path)
        if not vdb.exists():
            continue
        vs = VectorStore(vdb)
        try:
            results.extend(search(query, embedder, vs, top_k=top_k))
        finally:
            vs.close()
    results.sort(key=lambda r: r.get("rerank_score", r.get("score", 0.0)), reverse=True)
    return JSONResponse({"results": results[:top_k], "total": len(results)})


def _chunks_for(project: str, query: str, top_k: int = FINAL_TOP_K) -> list[dict]:
    """Return vector-search chunks for query across federation; empty list if not indexed."""
    if not project:
        return []
    from opencode_search.daemon.federation import expand_federation
    from opencode_search.embed.embedder import Embedder
    from opencode_search.index.store import VectorStore
    from opencode_search.query.search import search
    embedder = Embedder()
    embedder.warmup()
    results: list[dict] = []
    for p in expand_federation(project):
        vdb = project_vector_db(p)
        if not vdb.exists():
            continue
        vs = VectorStore(vdb)
        try:
            results.extend(search(query, embedder, vs, top_k=top_k))
        finally:
            vs.close()
    results.sort(key=lambda r: r.get("rerank_score", r.get("score", 0.0)), reverse=True)
    return results[:top_k]


async def _api_ask_get(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    query = request.query_params.get("query", "")
    scope = request.query_params.get("scope", "all")
    if not query:
        return JSONResponse({"error": "query required"}, status_code=400)
    if not project or not project_graph_db(project).exists():
        return JSONResponse({"answer": "Project not indexed.", "scope": scope})
    from opencode_search.daemon.federation import expand_federation
    from opencode_search.graph.store import GraphStore
    from opencode_search.query.ask import ask
    chunks = _chunks_for(project, query)
    stores = [GraphStore(project_graph_db(p)) for p in expand_federation(project) if project_graph_db(p).exists()]
    try:
        return JSONResponse({"answer": ask(query, chunks, stores, scope=scope), "scope": scope})
    finally:
        for s in stores:
            s.close()


async def _api_feature(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    query = request.query_params.get("query", "")
    if not query:
        return JSONResponse({"error": "query required"}, status_code=400)
    if not project or not project_graph_db(project).exists():
        return JSONResponse({"answer": "Project not indexed."})
    from opencode_search.daemon.federation import expand_federation
    from opencode_search.graph.store import GraphStore
    from opencode_search.query.ask import ask
    chunks = _chunks_for(project, query)
    stores = [GraphStore(project_graph_db(p)) for p in expand_federation(project) if project_graph_db(p).exists()]
    try:
        return JSONResponse({"answer": ask(query, chunks, stores, scope="feature")})
    finally:
        for s in stores:
            s.close()


async def _api_patterns(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    from pathlib import Path

    from opencode_search.kb.patterns import detect_patterns
    return JSONResponse(detect_patterns(Path(project)))


async def _api_analyze_patterns(request: Request) -> JSONResponse:
    body = await request.json()
    project_path = body.get("project_path", "")
    if not project_path:
        return JSONResponse({"error": "project_path required"}, status_code=400)
    from pathlib import Path

    from opencode_search.kb.patterns import detect_patterns
    return JSONResponse({"status": "ok", "patterns": detect_patterns(Path(project_path))})


async def _api_suggested_questions(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    if not project:
        return JSONResponse({"error": "project required"}, status_code=400)
    from opencode_search.daemon.federation import federated_map
    rows = [r for _, rs in federated_map(project, lambda gs: gs.conn.execute(
        "SELECT title FROM communities ORDER BY member_count DESC LIMIT 5"
    ).fetchall()) for r in rs]
    qs = list(dict.fromkeys(f"How does {r[0]} work?" for r in rows if r[0]))[:5]
    return JSONResponse({"questions": qs})


async def _api_classify(request: Request) -> JSONResponse:
    body = await request.json()
    message = body.get("message", "")
    if not message:
        return JSONResponse({"error": "message required"}, status_code=400)
    from opencode_search.query.chat_router import classify_intent
    intent = classify_intent(message)
    return JSONResponse({"intent": intent, "confidence": 0.9, "reason": "keyword-classified"})


def register(app) -> None:
    app.add_route("/api/search", _api_search_get, methods=["GET"])
    app.add_route("/api/ask", _api_ask_get, methods=["GET"])
    app.add_route("/api/feature", _api_feature, methods=["GET"])
    app.add_route("/api/patterns", _api_patterns, methods=["GET"])
    app.add_route("/api/analyze_patterns", _api_analyze_patterns, methods=["POST"])
    app.add_route("/api/suggested_questions", _api_suggested_questions, methods=["GET"])
    app.add_route("/api/classify", _api_classify, methods=["POST"])
