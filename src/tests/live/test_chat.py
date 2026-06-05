"""Live chat stream tests — require daemon at :8765 with an indexed project.

Tests POST /api/chat_stream which returns text/event-stream SSE.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.live, pytest.mark.slow]

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
