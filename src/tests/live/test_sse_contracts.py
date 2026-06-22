"""SSE event grammar contracts — chat_stream + events/stream.

Verifies the event sequence:
  chat_stream POST → chunks of {type: thinking|token|done, ...}
                   → done event has model_used ∈ {haiku|deepseek} + sources list
  events/stream GET → text/event-stream, first data contains "connected"

No mocks. Requires daemon at :8765 with ≥1 indexed project.
LLM tests are @slow (full round-trip to Anthropic/DeepSeek).
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.live

_ALLOWED_MODELS = {"claude-haiku-4-5", "deepseek-v4-flash", "deepseek-v4-flash-fallback"}


def _any_project():
    from opencode_search.core.registry import list_projects
    p = next((e.path for e in list_projects() if e.enabled), None)
    assert p, "At least one enabled project required"
    return p


def _collect_chat_events(live_client, project: str, msg: str, timeout: int = 60) -> list[dict]:
    events: list[dict] = []
    r = live_client.post(
        "/api/chat_stream",
        json={"message": msg, "project": project},
        stream=True, timeout=(5, timeout),
    )
    assert r.status_code == 200, f"chat_stream: {r.status_code} {r.text[:120]}"
    for line in r.iter_lines(decode_unicode=True):
        if line and line.startswith("data:"):
            try:
                ev = json.loads(line[5:].strip())
                events.append(ev)
                if ev.get("type") == "done":
                    break
            except json.JSONDecodeError:
                pass
    r.close()
    return events


def test_events_stream_sse_header(live_client):
    """GET /api/events/stream → text/event-stream content-type."""
    r = live_client.get("/api/events/stream", stream=True, timeout=3)
    ct = r.headers.get("content-type", "")
    r.close()
    assert r.status_code == 200
    assert "text/event-stream" in ct, f"Must be SSE; got {ct!r}"


def test_events_stream_connected_event(live_client):
    """events/stream must emit a 'connected' event within the read window."""
    body = b""
    r = live_client.get("/api/events/stream", stream=True, timeout=5)
    for chunk in r.iter_content(chunk_size=1024):
        body += chunk
        if b"connected" in body:
            break
    r.close()
    assert b"connected" in body, "events/stream must emit 'connected' SSE event"


@pytest.mark.slow
def test_chat_stream_done_event_present(live_client):
    """chat_stream must end with a 'done' typed event."""
    p = _any_project()
    events = _collect_chat_events(live_client, p, "What is this codebase?")
    types = [e.get("type") for e in events]
    assert "done" in types, f"chat_stream must emit done; got types={types}"


@pytest.mark.slow
def test_chat_stream_done_not_first(live_client):
    """done must not be the first event — at least one prior event (thinking or token)."""
    p = _any_project()
    events = _collect_chat_events(live_client, p, "What is this codebase?")
    types = [e.get("type") for e in events]
    assert "done" in types
    assert types.index("done") > 0, "done must not be the first SSE event"


@pytest.mark.slow
def test_chat_stream_model_in_allowed_set(live_client):
    """done event model_used must be haiku or deepseek (no codex, no random models)."""
    p = _any_project()
    events = _collect_chat_events(live_client, p, "List the main packages.")
    done_evs = [e for e in events if e.get("type") == "done"]
    assert done_evs, "No done event received"
    # done event uses "model" (string); "model_used" is a legacy list key
    raw = done_evs[0].get("model_used") or done_evs[0].get("model", "")
    model_used = raw if isinstance(raw, list) else ([raw] if raw else [])
    assert any(
        m in _ALLOWED_MODELS or any(x in m for x in ("haiku", "deepseek"))
        for m in model_used
    ), f"model_used must be haiku or deepseek; got {model_used}"


@pytest.mark.slow
def test_chat_stream_done_has_sources(live_client):
    """done event must include a sources list (context provenance)."""
    p = _any_project()
    events = _collect_chat_events(live_client, p, "What does this project do?")
    done_evs = [e for e in events if e.get("type") == "done"]
    assert done_evs, "No done event"
    done = done_evs[0]
    assert "sources" in done, f"done event must have sources; got keys={list(done)}"
    assert isinstance(done["sources"], list), "sources must be a list"
