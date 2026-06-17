"""chat_stream (SSE) route — codex/gpt-5.4-mini primary → claude-haiku-4-5 fallback."""
from __future__ import annotations

import asyncio
import json
import shutil

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from opencode_search.core.config import (
    QUERY_LLM_FALLBACK_MODEL,
    QUERY_LLM_MODEL,
)

_CODEX = shutil.which("codex")
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
    from opencode_search.query.search import search as _search

    gdb = project_graph_db(project_path)
    vdb = project_vector_db(project_path)
    if not gdb.exists() or not vdb.exists():
        return "", []

    cache_dir = index_dir(project_path) / "ask_cache"
    cached = _cache_get(cache_dir, f"chat2:{query}")
    if cached:
        d = json.loads(cached)
        return d["a"], d["s"]

    embedder = get_embedder()
    gs = GraphStore(gdb)
    vs = VectorStore(vdb)
    try:
        chunks = _search(query, embedder, vs, scope="all", top_k=8)
        answer = compose_answer(query, chunks, [gs], scope="all")
        sources = list(dict.fromkeys(c["path"] for c in chunks[:4]))
        _cache_set(cache_dir, f"chat2:{query}", json.dumps({"a": answer, "s": sources}), ttl_s=3600)
        return answer, sources
    finally:
        gs.close()
        vs.close()


async def _stream_answer(prompt: str):
    """Yield text chunks: codex (8s) → claude-haiku fallback. Never blocks the event loop."""
    if _CODEX:
        try:
            proc = await asyncio.create_subprocess_exec(
                _CODEX, "exec", "-m", QUERY_LLM_MODEL, "--ephemeral", prompt,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
            if stdout.strip():
                yield stdout.decode()
                return
        except (TimeoutError, OSError):
            pass
    if not _CLAUDE:
        raise RuntimeError("claude CLI not found — install Claude Code")
    proc = await asyncio.create_subprocess_exec(
        _CLAUDE, "-p", "--model", QUERY_LLM_FALLBACK_MODEL, prompt,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    while chunk := await proc.stdout.read(512):
        yield chunk.decode(errors="replace")
    await proc.wait()


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
        try:
            context, sources = await asyncio.wait_for(
                loop.run_in_executor(None, _build_context, project_path, message),
                timeout=12,
            )
        except (TimeoutError, Exception):
            context, sources = "", []

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

        try:
            async for chunk in _stream_answer(prompt):
                yield f"data: {json.dumps({'type': 'token', 'text': chunk})}\n\n".encode()
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n".encode()
        done_evt = {
            "type": "done", "done": True,
            "model": QUERY_LLM_FALLBACK_MODEL,
            "elapsed_ms": round((loop.time() - t0) * 1000),
            "sources": sources,
        }
        yield f"data: {json.dumps(done_evt)}\n\n".encode()

    return StreamingResponse(_gen(), media_type="text/event-stream")


def register(app) -> None:
    app.add_route("/api/chat_stream", _api_chat_stream, methods=["POST"])
