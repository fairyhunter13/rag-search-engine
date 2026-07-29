"""chat_stream (SSE) route — claude-haiku-4-5 only; no fallback; no local generative LLM."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from rag_search.core.config import QUERY_LLM_MODEL

log = logging.getLogger(__name__)

_CLAUDE = shutil.which("claude")

# A federated context build over the canonical 160-member root measures 18.0s cold and
# 18.2s warm through the daemon's own transport (2026-07-29, once 3bdbfb7 bounded the
# fan-out); a third run taken while load climbed 2.8 -> 4.4 reached 48s. The previous
# 12.0s could not cover that topology at all, so the *largest* project was the one
# guaranteed to expire and answer from nothing. Budget the loaded case, not the median.
_CONTEXT_BUDGET_S = 60.0


class ContextUnavailableError(RuntimeError):
    """Retrieval could not ground this question, so chat must not call the model.

    Chat has no tools and its system prompt says to answer using only the context
    provided, so an empty context does not merely degrade the answer — it asks the
    model to invent one. Every path that cannot produce grounding raises this instead
    of returning "", and _gen turns it into an SSE error event with no subprocess.
    """


def _pick_claude_env() -> dict[str, str] | None:
    """Pick the subscription profile with the most headroom for this chat call.

    Returns an env mapping with CLAUDE_CONFIG_DIR set, or None to inherit the
    ambient environment. None is the historical behaviour (always the default
    profile) and stays the fallback whenever selection is unavailable, so chat
    never breaks just because the vendored selector is missing.

    BLOCKING: on a cache miss this reads the usage endpoint (urlopen, 8s timeout,
    per-profile 5-minute cache). Callers on the event loop MUST run it in an
    executor — see _stream_answer.
    """
    try:
        from rag_search.core.claude_profiles import pick_profile, subprocess_env
        chosen = pick_profile()
        if not chosen:
            return None
        return subprocess_env(chosen)
    except Exception as exc:
        log.warning("chat profile selection unavailable (%s); using default profile", exc)
        return None


def _members_for(chunks: list[dict], all_paths: list[str]) -> list[str]:
    """Name the federation members that actually backed the answer.

    On the canonical topology an answer is assembled from up to 160 stores, and a bare
    chunk count says nothing about whether they came from one repo or five. Longest
    prefix first, so a member symlinked inside the root wins over the root itself.
    """
    roots = sorted(all_paths, key=len, reverse=True)
    seen: dict[str, None] = {}
    for c in chunks:
        p = c.get("path", "")
        for root in roots:
            if p == root or p.startswith(root.rstrip("/") + "/"):
                seen.setdefault(Path(root).name or root, None)
                break
    return list(seen)


def _build_context(project_path: str, query: str) -> tuple[str, list[str], list[str], int]:
    """Assemble grounding context, or raise ContextUnavailableError naming why it could not.

    Returns (context, sources, members, n_chunks). It never returns an empty context:
    returning "" is precisely what let chat answer ungrounded, so each empty case now
    raises with a reason a human can act on ("has no index yet"), not a silent "".
    """
    if not project_path:
        raise ContextUnavailableError("no project selected — pick a project to chat about its code")
    from contextlib import ExitStack, closing

    from rag_search.core.config import index_dir, project_graph_db, project_vector_db
    from rag_search.embed.embedder import get_embedder
    from rag_search.graph.store import GraphStore
    from rag_search.query.answer_cache import get as _cache_get
    from rag_search.query.answer_cache import set as _cache_set
    from rag_search.query.ask import compose_answer
    from rag_search.query.search import search_federation as _search_fed

    name = Path(project_path).name or project_path
    if not project_graph_db(project_path).exists() or not project_vector_db(project_path).exists():
        raise ContextUnavailableError(f"{name} has no index yet — index it before asking about it")

    from rag_search.daemon.federation import expand_federation
    cache_dir = index_dir(project_path) / "ask_cache"
    # chat3, not chat2: the payload now carries the grounding (members, chunk count) the
    # done event reports, so an entry written by the old shape must not be unpacked here.
    cached = _cache_get(cache_dir, f"chat3:{query}")
    if cached:
        d = json.loads(cached)
        return d["a"], d["s"], d["m"], d["n"]

    embedder = get_embedder()
    all_paths = expand_federation(project_path)
    # Paths for the vector side (`_search_fed` opens a batch at a time and closes each), ExitStack
    # for the graph side, which compose_answer does need open all at once — see ask.py.
    vector_dbs = [project_vector_db(p) for p in all_paths if project_vector_db(p).exists()]
    with ExitStack() as es:
        graph_stores = [
            es.enter_context(closing(GraphStore(project_graph_db(p))))
            for p in all_paths if project_graph_db(p).exists()
        ]
        chunks = _search_fed(query, embedder, vector_dbs, top_k=8)
        answer = compose_answer(query, chunks, graph_stores, scope="all")
        # Keyed on the assembled context, NOT on len(chunks): compose_answer also builds an
        # "## Architecture" section from the graph stores, so a query with no chunk hits can
        # still be genuinely grounded. Gating on the chunk count would reject a real answer.
        if not answer.strip():
            raise ContextUnavailableError(f"nothing in {name} matched this question")
        sources = list(dict.fromkeys(c["path"] for c in chunks[:4]))
        members = _members_for(chunks, all_paths)
        _cache_set(cache_dir, f"chat3:{query}",
                   json.dumps({"a": answer, "s": sources, "m": members, "n": len(chunks)}),
                   ttl_s=3600)
        return answer, sources, members, len(chunks)


def _drain(fut) -> None:
    """Swallow an abandoned context future's outcome.

    The budget bounds the RESPONSE, not the work. A thread already inside a sqlite call
    cannot be interrupted, and the `ctx_future.cancel()` this replaces was a no-op that
    merely read like one — it stopped nothing while making the expiry look handled. We
    let the thread finish and consume its result so asyncio does not log it unretrieved.
    """
    import contextlib
    with contextlib.suppress(Exception):
        fut.exception()


async def _stream_answer(prompt: str, model_used: list[str]):
    """Yield text chunks from claude-haiku-4-5. Raises RuntimeError if CLI absent or empty output.

    `claude -p` is the only generative engine in the system; there is no fallback.
    """
    if not _CLAUDE:
        raise RuntimeError(
            "claude CLI unavailable — dashboard chat requires claude-haiku-4-5, "
            "which is the only generative engine in the system"
        )
    model_used[0] = QUERY_LLM_MODEL
    # Spread chat load across subscription profiles instead of always billing the
    # default one. _pick_claude_env blocks on a usage lookup, so keep it off the
    # event loop; env=None means "inherit", i.e. the previous behaviour.
    loop = asyncio.get_running_loop()
    env = await loop.run_in_executor(None, _pick_claude_env)
    proc = await asyncio.create_subprocess_exec(
        _CLAUDE, "-p", "--model", QUERY_LLM_MODEL, prompt,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )
    output_bytes = b""
    while chunk := await proc.stdout.read(512):
        output_bytes += chunk
        yield chunk.decode(errors="replace")
    await proc.wait()
    if not output_bytes:
        raise RuntimeError(
            "claude-haiku-4-5 yielded empty output — dashboard chat has no fallback engine"
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
        deadline = loop.time() + _CONTEXT_BUDGET_S
        context, sources, members, n_chunks, reason = "", [], [], 0, ""
        while True:
            try:
                context, sources, members, n_chunks = await asyncio.wait_for(
                    asyncio.shield(ctx_future), timeout=2.0
                )
                break
            except TimeoutError:
                if loop.time() < deadline:
                    yield b'data: {"type":"thinking"}\n\n'
                    continue
                ctx_future.add_done_callback(_drain)
                reason = (f"context retrieval for this project exceeded its "
                          f"{_CONTEXT_BUDGET_S:.0f}s budget")
                break
            except ContextUnavailableError as exc:
                reason = str(exc)
                break
            except Exception as exc:
                log.warning("chat context build failed: %s", exc, exc_info=True)
                reason = f"context retrieval failed: {exc}"
                break

        def _done_event(model: str, *, grounded: bool) -> bytes:
            # `grounded` describes the CONTEXT, not the round trip: it says the answer was
            # assembled from retrieved material. A model failure after that surfaces as its
            # own error event rather than by retracting the grounding.
            evt = {
                "type": "done", "done": True,
                "model": model,
                "grounded": grounded,
                "chunks": n_chunks,
                "members": members,
                "sources": sources,
                "elapsed_ms": round((loop.time() - t0) * 1000),
            }
            return f"data: {json.dumps(evt)}\n\n".encode()

        if reason:
            # Retrieval-first: no context means no model call. The model has no tools to
            # recover with and a prompt that says "answer using only the context provided",
            # so the only thing an ungrounded call can produce is a confident invention.
            yield f"data: {json.dumps({'type': 'error', 'message': reason})}\n\n".encode()
            yield _done_event("", grounded=False)
            return

        sys_prompt = (
            "You are a helpful code intelligence assistant. Answer using only the context "
            "provided; do not invoke any external tools."
            f"\n\nProject context:\n{context}"
        )
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
        yield _done_event(model_used[0], grounded=True)

    return StreamingResponse(_gen(), media_type="text/event-stream")


def register(app) -> None:
    app.add_route("/api/chat_stream", _api_chat_stream, methods=["POST"])
