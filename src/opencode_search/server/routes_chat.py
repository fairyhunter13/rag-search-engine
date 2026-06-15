"""Chat (non-streaming) and chat_stream (SSE) routes — codex/gpt-5.4-mini ONLY."""
from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from opencode_search.core.config import (
    QUERY_LLM_FALLBACK_MODEL,
    QUERY_LLM_MODEL,
    QUERY_LLM_PROVIDER,
    QUERY_LLM_TIMEOUT,
    project_graph_db,
)


def _build_context(project_path: str) -> str:
    if not project_path:
        return ""
    gdb = project_graph_db(project_path)
    if not gdb.exists():
        return ""
    from opencode_search.graph.store import GraphStore
    gs = GraphStore(gdb)
    try:
        rows = gs.conn.execute(
            "SELECT summary FROM communities ORDER BY member_count DESC LIMIT 3"
        ).fetchall()
        return "\n".join(r[0] for r in rows if r[0])
    finally:
        gs.close()


def _call_llm(messages: list[dict], stream: bool = False):
    if QUERY_LLM_PROVIDER == "codex":
        try:
            from openai import OpenAI
            return OpenAI(timeout=QUERY_LLM_TIMEOUT).chat.completions.create(
                model=QUERY_LLM_MODEL, messages=messages, stream=stream
            )
        except Exception:
            pass
    import anthropic
    sys_msgs = [m["content"] for m in messages if m["role"] == "system"]
    user_msgs = [m for m in messages if m["role"] != "system"]
    return anthropic.Anthropic().messages.create(
        model=QUERY_LLM_FALLBACK_MODEL, max_tokens=2048,
        system=sys_msgs[0] if sys_msgs else "You are a helpful code assistant.",
        messages=user_msgs, stream=stream,
    )


async def _api_chat(request: Request) -> JSONResponse:
    body = await request.json()
    message = body.get("message", "")
    project_path = body.get("project_path", "")
    if not message:
        return JSONResponse({"error": "message required"}, status_code=400)
    from opencode_search.query.chat_router import route
    gdb = project_graph_db(project_path) if project_path else None
    from opencode_search.graph.store import GraphStore
    gs = GraphStore(gdb) if gdb and gdb.exists() else None
    try:
        answer = route(message, gs) if gs else f"No project indexed. Query: {message}"
    finally:
        if gs:
            gs.close()
    return JSONResponse({"answer": answer, "project": project_path})


async def _api_chat_stream(request: Request) -> Response:
    body = await request.json()
    message = body.get("message", "")
    project_path = body.get("project_path", "")
    if not message:
        return Response('data: {"error":"message required"}\n\ndata: {"done":true}\n\n',
                        media_type="text/event-stream", status_code=400)
    context = _build_context(project_path)
    sys_prompt = "You are a helpful code intelligence assistant."
    if context:
        sys_prompt += f"\n\nProject context:\n{context}"
    msgs = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": message}]

    async def _gen():
        try:
            for chunk in _call_llm(msgs, stream=True):
                if QUERY_LLM_PROVIDER == "codex":
                    delta = chunk.choices[0].delta.content or ""
                else:
                    delta = chunk.delta.text if hasattr(chunk, "delta") else ""
                if delta:
                    yield f"data: {json.dumps({'text': delta})}\n\n".encode()
        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n".encode()
        yield b'data: {"done":true}\n\n'

    return StreamingResponse(_gen(), media_type="text/event-stream")


def register(app) -> None:
    app.add_route("/api/chat", _api_chat, methods=["POST"])
    app.add_route("/api/chat_stream", _api_chat_stream, methods=["POST"])
