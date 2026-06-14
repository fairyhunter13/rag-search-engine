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
    paths = [project] if project else [p.path for p in list_projects() if p.enabled]
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
    results.sort(key=lambda r: r.get("score", 0), reverse=True)
    return JSONResponse({"results": results[:top_k], "total": len(results)})


def _chunks_for(project: str, query: str, top_k: int = FINAL_TOP_K) -> list[dict]:
    """Return vector-search chunks for query; empty list if project not indexed."""
    from opencode_search.embed.embedder import Embedder
    from opencode_search.index.store import VectorStore
    from opencode_search.query.search import search
    vdb = project_vector_db(project) if project else None
    if not vdb or not vdb.exists():
        return []
    embedder = Embedder()
    embedder.warmup()
    vs = VectorStore(vdb)
    try:
        return search(query, embedder, vs, top_k=top_k)
    finally:
        vs.close()


async def _api_ask_get(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    query = request.query_params.get("query", "")
    scope = request.query_params.get("scope", "all")
    if not query:
        return JSONResponse({"error": "query required"}, status_code=400)
    from opencode_search.graph.store import GraphStore
    from opencode_search.query.ask import ask
    gdb = project_graph_db(project) if project else None
    if not gdb or not gdb.exists():
        return JSONResponse({"answer": "Project not indexed.", "scope": scope})
    chunks = _chunks_for(project, query)
    gs = GraphStore(gdb)
    try:
        return JSONResponse({"answer": ask(query, chunks, gs, scope=scope), "scope": scope})
    finally:
        gs.close()


async def _api_feature(request: Request) -> JSONResponse:
    project = request.query_params.get("project", "")
    query = request.query_params.get("query", "")
    if not query:
        return JSONResponse({"error": "query required"}, status_code=400)
    from opencode_search.graph.store import GraphStore
    from opencode_search.query.ask import ask
    gdb = project_graph_db(project) if project else None
    if not gdb or not gdb.exists():
        return JSONResponse({"answer": "Project not indexed."})
    chunks = _chunks_for(project, query)
    gs = GraphStore(gdb)
    try:
        return JSONResponse({"answer": ask(query, chunks, gs, scope="feature")})
    finally:
        gs.close()


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
    gdb = project_graph_db(project)
    if not gdb.exists():
        return JSONResponse({"questions": []})
    from opencode_search.graph.store import GraphStore
    gs = GraphStore(gdb)
    try:
        comms = gs.conn.execute("SELECT title FROM communities ORDER BY node_count DESC LIMIT 5").fetchall()
        return JSONResponse({"questions": [f"How does {r['title']} work?" for r in comms]})
    finally:
        gs.close()


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
