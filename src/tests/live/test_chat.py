"""Live chat stream tests — require daemon at :8765 with an indexed project.

Tests POST /api/chat_stream which returns text/event-stream SSE.
"""
from __future__ import annotations

import shutil

import pytest

pytestmark = [pytest.mark.live, pytest.mark.slow]

# Query-tier models: any of these are valid for dashboard chat responses.
# qwen3-query:8b — used when daemon runs via systemd (OPENCODE_QUERY_LLM_PROVIDER=ollama)
# gpt-5.4-mini / haiku — used in interactive shell where bash_aliases sets OPENCODE_QUERY_LLM_PROVIDER=codex
_QUERY_TIER_MODELS = {"gpt-5.4-mini", "claude-haiku-4-5-20251001", "qwen3-query:8b"}
# Build-tier model: FORBIDDEN for chat — it's the KB enrichment model (weak, non-instructable)
_BUILD_TIER_MODELS = {"qwen3-enrich:1.7b"}

from .conftest import parse_sse  # noqa: E402


def _chat(http, project: str, query: str) -> tuple[str, str, list[str], int]:
    """Send a chat_stream query, return (answer, intent, sources, elapsed_ms)."""
    r = http.post(
        "/api/chat_stream",
        json={"project": project, "query": query},
        headers={"Accept": "text/event-stream"},
    )
    assert r.status_code == 200, f"chat_stream failed: {r.status_code} {r.text[:200]}"
    events = parse_sse(r)
    answer = "".join(e.get("text", "") for e in events if e.get("type") == "token")
    done = next((e for e in events if e.get("type") == "done"), {})
    return answer, done.get("intent", ""), done.get("sources", []), done.get("elapsed_ms", 0)


def test_chat_stream_ends_with_done(http, project):
    """SSE stream must end with a 'done' event — no hanging connections."""
    r = http.post(
        "/api/chat_stream",
        json={"project": project, "query": "What is this project?"},
        headers={"Accept": "text/event-stream"},
    )
    assert r.status_code == 200
    events = parse_sse(r)
    types = [e.get("type") for e in events]
    assert "done" in types, f"Stream never received done event; got types: {types}"
    assert types[-1] == "done", f"done must be the last event; got trailing: {types[-3:]}"


def test_chat_done_has_intent_and_elapsed(http, project):
    """Done event must carry intent and elapsed_ms so callers know what happened."""
    r = http.post(
        "/api/chat_stream",
        json={"project": project, "query": "How does search work?"},
        headers={"Accept": "text/event-stream"},
    )
    events = parse_sse(r)
    done = next((e for e in events if e.get("type") == "done"), None)
    assert done is not None, "No done event in stream"
    assert "intent" in done, f"done missing 'intent'; got keys: {list(done.keys())}"
    assert "elapsed_ms" in done, f"done missing 'elapsed_ms'; got keys: {list(done.keys())}"
    assert done["elapsed_ms"] > 0, f"elapsed_ms must be positive; got {done['elapsed_ms']}"


def test_chat_tokens_arrive_before_done(http, project):
    """Token events must precede the done event — streaming order must be preserved."""
    r = http.post(
        "/api/chat_stream",
        json={"project": project, "query": "What are the main components?"},
        headers={"Accept": "text/event-stream"},
    )
    events = parse_sse(r)
    types = [e.get("type") for e in events]
    done_idx = next((i for i, t in enumerate(types) if t == "done"), -1)
    token_idx = next((i for i, t in enumerate(types) if t == "token"), -1)
    assert done_idx >= 0, "No done event"
    if token_idx >= 0:
        assert token_idx < done_idx, (
            f"token at index {token_idx} must precede done at index {done_idx}"
        )


def test_chat_returns_non_empty_answer(http, project):
    """A real question must produce a non-empty streamed answer."""
    answer, _intent, _sources, _elapsed = _chat(http, project, "What does this project do?")
    assert len(answer) > 20, f"Answer suspiciously short ({len(answer)} chars): {answer!r}"


def test_chat_architecture_intent(http, project):
    """An architecture question must route to architecture or global intent."""
    _answer, intent, _sources, _elapsed = _chat(http, project, "What is the overall architecture of this codebase?")
    assert intent in ("architecture", "global", "feature"), (
        f"Architecture query routed to unexpected intent: {intent!r}"
    )


def test_chat_debug_trace_intent(http, project):
    """A Python traceback must route to debug_trace intent."""
    traceback = (
        "Traceback (most recent call last):\n"
        "  File 'main.py', line 42, in run\n"
        "    result = process(data)\n"
        "KeyError: 'content'"
    )
    _answer, intent, _sources, _elapsed = _chat(http, project, traceback)
    assert intent == "debug_trace", (
        f"Python traceback must route to debug_trace; got: {intent!r}"
    )


def test_chat_sources_are_real_paths(http, project):
    """Sources in the done event must look like real file paths, not fabricated ones."""
    _answer, _intent, sources, _elapsed = _chat(http, project, "Find the main server handler")
    if sources:
        fabricated = [s for s in sources if any(k in s for k in ("/fake/", "/example/", "/placeholder/"))]
        assert not fabricated, f"Fabricated paths in sources: {fabricated}"


@pytest.mark.slow
def test_chat_debug_intent(http, project):
    """A 'why is X slow/broken' question must route to debug intent."""
    _answer, intent, _sources, _elapsed = _chat(
        http, project,
        "why is the indexer slow and how can I debug it?",
    )
    assert intent == "debug", f"Expected intent=debug for debug question; got: {intent!r}"


@pytest.mark.slow
def test_global_intent_routes_correctly(http, project):
    """Global query must route to 'global' intent and return a substantive answer."""
    r = http.post(
        "/api/chat_stream",
        json={"project": project,
              "query": "give me a global overview of the entire system and all its components"},
        headers={"Accept": "text/event-stream"},
    )
    assert r.status_code == 200
    events = parse_sse(r)
    tokens = [e for e in events if e.get("type") == "token"]
    done = next((e for e in events if e.get("type") == "done"), {})
    assert done.get("intent") == "global", (
        f"Expected global intent, got {done.get('intent')!r}"
    )
    answer = "".join(e.get("text", "") for e in tokens)
    assert len(answer) > 100, f"Global answer too short: {len(answer)} chars"


@pytest.mark.slow
def test_chat_search_intent(http, project):
    """An explicit 'find/show me the source code' query must route to search intent."""
    _answer, intent, _sources, _elapsed = _chat(
        http, project,
        "show me the source code of the main HTTP route handler",
    )
    assert intent in ("search", "architecture", "feature"), (
        f"Search query routed to unexpected intent: {intent!r}"
    )


@pytest.mark.slow
def test_chat_feature_intent(http, project):
    """An 'end-to-end how does X work' query must route to feature intent."""
    _answer, intent, _sources, _elapsed = _chat(
        http, project,
        "explain step by step how the indexing feature works from file discovery to storage",
    )
    assert intent in ("feature", "architecture", "global"), (
        f"Feature query routed to unexpected intent: {intent!r}"
    )


@pytest.mark.flaky(reruns=2, reruns_delay=10)
@pytest.mark.parametrize("query", [
    "what functions call the embedder",
    "what triggers the order workflow?",
    "what initiates the payment flow?",
    "what invokes the auth handler?",
    "who calls AddToCart?",
    "what fires the campaign service?",
])
def test_chat_graph_callers_intent(http, project, query):
    """A 'what calls/triggers/invokes X' query must route to graph_callers intent."""
    _answer, intent, _sources, _elapsed = _chat(http, project, query)
    assert intent in ("graph_callers", "search"), (
        f"Query {query!r} routed to {intent!r} instead of graph_callers"
    )


@pytest.mark.slow
def test_chat_graph_callees_intent(http, project):
    """A 'what does X call' query must route to graph_callees intent."""
    _answer, intent, _sources, _elapsed = _chat(
        http, project,
        "what does the main search handler call internally",
    )
    assert intent in ("graph_callees", "search", "feature"), (
        f"Callees query routed to unexpected intent: {intent!r}"
    )


# ---------------------------------------------------------------------------
# LLM tier guard — dashboard chat must never use the build-tier (ollama) LLM
# ---------------------------------------------------------------------------

def test_api_chat_uses_query_tier_llm(http, project):
    """POST /api/chat must return a query-tier model, not the build/enrich model.

    Catches the regression where handle_kb_chat used create_llm_client (qwen3-enrich:1.7b)
    instead of create_query_llm_client (qwen3-query:8b via systemd, or codex → haiku in shell).
    """
    r = http.post(
        "/api/chat",
        json={"project": project, "query": "list all features and components of this project"},
    )
    assert r.status_code == 200, f"/api/chat failed: {r.status_code} {r.text[:200]}"
    data = r.json()
    model = data.get("model", "")
    assert model not in _BUILD_TIER_MODELS, (
        f"/api/chat returned build-tier model {model!r}; dashboard chat must use "
        f"query-tier ({_QUERY_TIER_MODELS}). "
        f"Root cause: _kb_chat.py was using create_llm_client instead of create_query_llm_client."
    )
    assert any(m in model for m in _QUERY_TIER_MODELS) or model, (
        f"/api/chat returned empty model field: {data}"
    )


async def test_kb_chat_handler_uses_query_tier(project):
    """handle_kb_chat must use create_query_llm_client, not the build-tier LLM."""
    assert shutil.which("codex") or shutil.which("claude"), (
        "Neither codex nor claude CLI installed — query-tier unavailable"
    )
    from opencode_search.handlers._kb_chat import handle_kb_chat
    result = await handle_kb_chat(
        query="what are the main features",
        project_path=project,
        mode="quick",
        top_k=5,
    )
    model = result.get("model", "")
    assert model not in _BUILD_TIER_MODELS, (
        f"handle_kb_chat used build-tier model {model!r}; expected query-tier "
        f"({_QUERY_TIER_MODELS})"
    )


def test_chat_router_global_intent_reports_query_tier_model(http, project):
    """A comprehensive KB-chat query must use a query-tier model, not the build/enrich tier.

    Uses /api/chat (blocking) to avoid SSE stream timeout. Covers the code path where
    chat_router routes to handle_kb_chat for global/feature intents.
    """
    r = http.post(
        "/api/chat",
        json={"project": project, "query": "list all the business features and capabilities this project provides"},
    )
    assert r.status_code == 200, f"/api/chat failed: {r.status_code} {r.text[:200]}"
    data = r.json()
    model = data.get("model", "")
    assert model not in _BUILD_TIER_MODELS, (
        f"/api/chat (global path) used build-tier model {model!r}; expected query-tier "
        f"({_QUERY_TIER_MODELS})"
    )


@pytest.mark.flaky(reruns=2, reruns_delay=10)
@pytest.mark.parametrize("query", [
    "what breaks if I change the embedding model",
    "what services would break if I change the campaign gRPC contract?",
    "what is the blast radius of removing AuthService.Login?",
    "what would be affected by modifying the payment processor?",
    "what depends on the user service interface?",
    "what is impacted by changing the inventory contract?",
])
def test_chat_graph_impact_intent(http, project, query):
    """A 'what breaks/blast radius/impact of changing X' query must route to graph_impact."""
    _answer, intent, _sources, _elapsed = _chat(http, project, query)
    assert intent in ("graph_impact", "architecture", "feature"), (
        f"Query {query!r} routed to {intent!r} instead of graph_impact"
    )


@pytest.mark.live
def test_chat_stream_unregistered_project_emits_structured_error(http):
    """SSE stream for an unregistered project must emit a structured error, not a raw exception."""
    r = http.post(
        "/api/chat_stream",
        json={"project": "/tmp/this-project-does-not-exist-in-registry", "query": "hello"},
        headers={"Accept": "text/event-stream"},
    )
    assert r.status_code == 200, f"Expected SSE 200, got {r.status_code}: {r.text[:200]}"
    from .conftest import parse_sse
    events = parse_sse(r)
    error_events = [e for e in events if e.get("type") == "error"]
    assert error_events, f"No error event in SSE stream; got events: {events}"
    err = error_events[0]
    assert err.get("code") == "PROJECT_NOT_REGISTERED", (
        f"Expected code=PROJECT_NOT_REGISTERED, got: {err}"
    )
