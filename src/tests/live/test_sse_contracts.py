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

from tests.live._sample_workspace import SampleWorkspace

pytestmark = pytest.mark.live

_ALLOWED_MODELS = {"claude-haiku-4-5"}  # chat lane is Haiku-only (EC2)


@pytest.fixture(scope="module")
def project(sample_workspace: SampleWorkspace) -> str:
    return sample_workspace.promo


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
def test_chat_stream_done_event_present(live_client, project):
    """chat_stream must end with a 'done' typed event."""
    events = _collect_chat_events(live_client, project, "What is this codebase?")
    types = [e.get("type") for e in events]
    assert "done" in types, f"chat_stream must emit done; got types={types}"


@pytest.mark.slow
def test_chat_stream_done_not_first(live_client, project):
    """done must not be the first event — at least one prior event (thinking or token)."""
    events = _collect_chat_events(live_client, project, "What is this codebase?")
    types = [e.get("type") for e in events]
    assert "done" in types
    assert types.index("done") > 0, "done must not be the first SSE event"


@pytest.mark.slow
def test_chat_stream_model_in_allowed_set(live_client, project):
    """done event model must be claude-haiku-4-5 (chat lane is Haiku-only — EC2)."""
    events = _collect_chat_events(live_client, project, "List the main packages.")
    done_evs = [e for e in events if e.get("type") == "done"]
    assert done_evs, "No done event received"
    # done event uses "model" (string)
    model = done_evs[0].get("model", "")
    assert model in _ALLOWED_MODELS or "haiku" in model, (
        f"chat model must be haiku-only; got {model!r}"
    )


@pytest.mark.slow
def test_chat_stream_done_has_sources(live_client, project):
    """done event must include a sources list (context provenance)."""
    events = _collect_chat_events(live_client, project, "What does this project do?")
    done_evs = [e for e in events if e.get("type") == "done"]
    assert done_evs, "No done event"
    done = done_evs[0]
    assert "sources" in done, f"done event must have sources; got keys={list(done)}"
    assert isinstance(done["sources"], list), "sources must be a list"
