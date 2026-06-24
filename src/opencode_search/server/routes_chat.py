"""chat_stream (SSE) route — claude-haiku-4-5 only; no DeepSeek fallback; no local generative LLM."""
from __future__ import annotations

import asyncio
import json
import shutil

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from opencode_search.core.config import QUERY_LLM_MODEL

_CLAUDE = shutil.which("claude")


def _build_context(project_path: str, query: str) -> tuple[str, list[str]]:
    if not project_path:
        return "", []
    from opencode_search.core.config import index_dir, project_graph_db, project_vector_db
    from opencode_search.embed.embedder import get_embedder
    from opencode_search.graph.store import GraphStore
    from opencode_search.index.store import VectorStore
    from opencode_search.kb.answer_cache import get as _cache_get
    from opencode_search.kb.answer_cache import set as _cache_set
    from opencode_search.query.ask import compose_answer
    from opencode_search.query.search import search_federation as _search_fed

    if not project_graph_db(project_path).exists() or not project_vector_db(project_path).exists():
        return "", []

    from opencode_search.daemon.federation import expand_federation
    cache_dir = index_dir(project_path) / "ask_cache"
    cached = _cache_get(cache_dir, f"chat2:{query}")
    if cached:
        d = json.loads(cached)
        return d["a"], d["s"]

    embedder = get_embedder()
    all_paths = expand_federation(project_path)
    graph_stores = [GraphStore(project_graph_db(p)) for p in all_paths if project_graph_db(p).exists()]
    vector_stores = [VectorStore(project_vector_db(p)) for p in all_paths if project_vector_db(p).exists()]
    try:
        chunks = _search_fed(query, embedder, vector_stores, top_k=8)
        answer = compose_answer(query, chunks, graph_stores, scope="all")
        sources = list(dict.fromkeys(c["path"] for c in chunks[:4]))
        _cache_set(cache_dir, f"chat2:{query}", json.dumps({"a": answer, "s": sources}), ttl_s=3600)
        return answer, sources
    finally:
        for vs in vector_stores:
            vs.close()
        for gs in graph_stores:
            gs.close()


async def _stream_answer(prompt: str, model_used: list[str]):
    """Yield text chunks from claude-haiku-4-5. Raises RuntimeError if CLI absent or empty output.

    DeepSeek is the KB-enrichment-exclusive engine (HR12); it has no role in dashboard chat.
    """
    if not _CLAUDE:
        raise RuntimeError(
            "claude CLI unavailable — dashboard chat requires claude-haiku-4-5 "
            "(DeepSeek is KB-enrichment-only)"
        )
    model_used[0] = QUERY_LLM_MODEL
    proc = await asyncio.create_subprocess_exec(
        _CLAUDE, "-p", "--model", QUERY_LLM_MODEL, prompt,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    output_bytes = b""
    while chunk := await proc.stdout.read(512):
        output_bytes += chunk
        yield chunk.decode(errors="replace")
    await proc.wait()
    if not output_bytes:
        raise RuntimeError(
            "claude-haiku-4-5 yielded empty output — dashboard chat requires claude-haiku-4-5 "
            "(DeepSeek is KB-enrichment-only)"
        )


async def _api_chat_stream(request: Request) -> Response:
    body = await request.json()
    message = body.get("message") or body.get("query", "")
    project_path = body.get("project_path") or body.get("project", "")
    history = body.get("history", [])
    if not message:
        return Response('data: {"type":"error","message":"message required"}\n\ndata: {"type":"done"}\n\n',
                        media_type="text/event-stream", status_code=400)
    loop = asyncio.get_running_loop()
    t0 = loop.time()

    async def _gen():
        yield b'data: {"type":"thinking"}\n\n'
        ctx_future = loop.run_in_executor(None, _build_context, project_path, message)
        deadline = loop.time() + 12.0
        context, sources = "", []
        while True:
            try:
                context, sources = await asyncio.wait_for(asyncio.shield(ctx_future), timeout=2.0)
                break
            except TimeoutError:
                if loop.time() < deadline:
                    yield b'data: {"type":"thinking"}\n\n'
                else:
                    ctx_future.cancel()
                    break
            except Exception:
                break

        sys_prompt = "You are a helpful code intelligence assistant. Answer using only the context provided; do not invoke any external tools."
        if context:
            sys_prompt += f"\n\nProject context:\n{context}"
        if history:
            hist_str = "".join(
                f"\n{'User' if t.get('role') == 'user' else 'Assistant'}: {t.get('content', '')[:500]}"
                for t in history[-6:]
            )
            sys_prompt += f"\n\nRecent conversation:{hist_str}"
        prompt = f"{sys_prompt}\n\n{message}"

        model_used = [QUERY_LLM_MODEL]
        try:
            async for chunk in _stream_answer(prompt, model_used):
                yield f"data: {json.dumps({'type': 'token', 'text': chunk})}\n\n".encode()
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n".encode()
        done_evt = {
            "type": "done", "done": True,
            "model": model_used[0],
            "elapsed_ms": round((loop.time() - t0) * 1000),
            "sources": sources,
        }
        yield f"data: {json.dumps(done_evt)}\n\n".encode()

    return StreamingResponse(_gen(), media_type="text/event-stream")


def register(app) -> None:
    app.add_route("/api/chat_stream", _api_chat_stream, methods=["POST"])
