"""chat_stream (SSE) route — codex/gpt-5.4-mini primary → claude-haiku-4-5 fallback."""
from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from opencode_search.core.config import (
    QUERY_LLM_FALLBACK_MODEL,
    QUERY_LLM_MODEL,
    QUERY_LLM_PROVIDER,
    QUERY_LLM_TIMEOUT,
    project_graph_db,
)


def _build_context(project_path: str, query: str) -> str:
    if not project_path:
        return ""
    gdb = project_graph_db(project_path)
    if not gdb.exists():
        return ""
    from opencode_search.graph.store import GraphStore
    from opencode_search.query.ask import _top_communities_semantic
    gs = GraphStore(gdb)
    try:
        return _top_communities_semantic(query, [gs], top_k=5)
    finally:
        gs.close()


def _chat_tokens(messages: list[dict]):
    """Yield tokens: codex/gpt-5.4-mini primary → claude-haiku-4-5 on error or empty."""
    emitted = False
    if QUERY_LLM_PROVIDER == "codex":
        try:
            from openai import OpenAI
            for chunk in OpenAI(timeout=QUERY_LLM_TIMEOUT).chat.completions.create(
                model=QUERY_LLM_MODEL, messages=messages, stream=True
            ):
                t = chunk.choices[0].delta.content or ""
                if t:
                    emitted = True
                    yield t
        except Exception:
            pass
    if emitted:
        return
    import anthropic
    sys_msgs = [m["content"] for m in messages if m["role"] == "system"]
    user_msgs = [m for m in messages if m["role"] != "system"]
    with anthropic.Anthropic().messages.stream(
        model=QUERY_LLM_FALLBACK_MODEL, max_tokens=2048,
        system=sys_msgs[0] if sys_msgs else "You are a helpful code assistant.",
        messages=user_msgs,
    ) as stream:
        for t in stream.text_stream:
            if t:
                yield t


async def _api_chat_stream(request: Request) -> Response:
    body = await request.json()
    message = body.get("message") or body.get("query", "")
    project_path = body.get("project_path") or body.get("project", "")
    if not message:
        return Response('data: {"type":"error","message":"message required"}\n\ndata: {"type":"done"}\n\n',
                        media_type="text/event-stream", status_code=400)
    context = _build_context(project_path, message)
    sys_prompt = "You are a helpful code intelligence assistant."
    if context:
        sys_prompt += f"\n\nProject context:\n{context}"
    msgs = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": message}]

    async def _gen():
        try:
            for t in _chat_tokens(msgs):
                yield f"data: {json.dumps({'type': 'token', 'text': t})}\n\n".encode()
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n".encode()
        yield b'data: {"type":"done","done":true}\n\n'

    return StreamingResponse(_gen(), media_type="text/event-stream")


def register(app) -> None:
    app.add_route("/api/chat_stream", _api_chat_stream, methods=["POST"])
