"""SSE event grammar contracts — chat_stream.

Verifies the event sequence:
  chat_stream POST → chunks of {type: thinking|token|done, ...}
                   → done event has model_used == claude-haiku-4-5 + sources list

No mocks. Requires daemon at :8765 with ≥1 indexed project.
LLM tests are @slow (full round-trip to Anthropic).

The `{haiku|deepseek}` union above described a fallback the chat lane stopped having long
before tier 3 went: `_ALLOWED_MODELS` has been Haiku-only, so the docstring was advertising
a second model that test_chat_model_is_haiku would already have failed on.
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


# The two /api/events/stream tests left with tier 3 along with the route. Its only publisher was
# the pipeline job runner (build_wiki/docgen/okf); with that gone the stream could only ever emit
# its own "connected" and "keepalive" frames, so both tests would have kept passing over a bus with
# nothing on it — an assertion that survives the thing it was written to observe. chat_stream is
# the surviving SSE surface and the rest of this module covers it.


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
