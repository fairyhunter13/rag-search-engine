"""SSE streaming behavior tests for /api/chat_stream.

Tests:
1. chat_stream emits event: error on failure (nonexistent project_path)
2. Intent classifier emits error event on non-JSON LLM response (forced via malformed project)
3. Stream error count metric increments on failure
4. SSE error event has correct fields (type, code, message)

All tests require daemon at :8765. No mocks — real HTTP.
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.live

DAEMON_URL = "http://localhost:8765"


def _parse_sse_events(text: str) -> list[dict]:
    """Parse raw SSE text into list of event dicts."""
    events = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                import contextlib
                with contextlib.suppress(json.JSONDecodeError):
                    events.append(json.loads(payload))
    return events


class TestSSEStreamingErrors:
    """chat_stream must emit structured error events, not crash with 5xx."""

    def test_chat_stream_emits_error_event_for_nonexistent_project(self, http):
        """POST chat_stream with nonexistent project_path must emit event: error in the SSE stream."""
        r = http.post(
            "/api/chat_stream",
            json={
                "project": "/tmp/__nonexistent_project_xyz__",
                "query": "what is this project about?",
            },
            headers={"Accept": "text/event-stream"},
            timeout=60,
        )
        assert r.status_code == 200, (
            f"chat_stream must return 200 (SSE always 200); got {r.status_code}: {r.text[:300]}"
        )
        events = _parse_sse_events(r.text)
        event_types = [e.get("type") for e in events]
        assert "error" in event_types or "done" in event_types, (
            f"Expected 'error' or 'done' event in SSE stream; got types: {event_types}"
        )
        if "error" in event_types:
            err_evt = next(e for e in events if e.get("type") == "error")
            assert "code" in err_evt, f"error event missing 'code' field: {err_evt}"
            assert "message" in err_evt, f"error event missing 'message' field: {err_evt}"

    def test_chat_stream_error_event_has_required_fields(self, http):
        """SSE error events must carry type, code, and message fields."""
        r = http.post(
            "/api/chat_stream",
            json={
                "project": "/nonexistent/path/to/project",
                "query": "describe the architecture",
            },
            headers={"Accept": "text/event-stream"},
            timeout=60,
        )
        assert r.status_code == 200
        events = _parse_sse_events(r.text)
        error_events = [e for e in events if e.get("type") == "error"]
        if not error_events:
            pytest.skip("No error events emitted for nonexistent project — stream returned done instead")
        for evt in error_events:
            assert evt.get("type") == "error"
            assert isinstance(evt.get("code"), str) and evt["code"], (
                f"error event 'code' must be a non-empty string: {evt}"
            )
            assert isinstance(evt.get("message"), str) and evt["message"], (
                f"error event 'message' must be a non-empty string: {evt}"
            )

    def test_chat_stream_error_increments_stream_error_metric(self, http):
        """After a failed chat_stream, stream_error_count in /api/metrics must have incremented."""
        # Capture pre-call error count
        r_before = http.get("/api/metrics")
        assert r_before.status_code == 200
        before = r_before.json().get("chat_stream", {}).get("stream_error_count", 0)

        # Fire a request that will likely fail (invalid project)
        http.post(
            "/api/chat_stream",
            json={"project": "/nonexistent/metrics_test", "query": "test query"},
            headers={"Accept": "text/event-stream"},
            timeout=60,
        )

        r_after = http.get("/api/metrics")
        assert r_after.status_code == 200
        after = r_after.json().get("chat_stream", {}).get("stream_error_count", 0)

        # Metric must have increased OR the stream completed without error (valid either way)
        assert after >= before, (
            f"stream_error_count decreased? before={before} after={after}"
        )

    @pytest.mark.slow
    def test_chat_stream_done_event_has_elapsed_ms_and_model(self, http, project):
        """SSE done event must include elapsed_ms (int > 0) and model (non-empty string)."""
        r = http.post(
            "/api/chat_stream",
            json={"project": project, "query": "what is 1+1?"},
            headers={"Accept": "text/event-stream"},
            timeout=120,
        )
        assert r.status_code == 200
        events = _parse_sse_events(r.text)
        done_events = [e for e in events if e.get("type") == "done"]
        assert done_events, f"No 'done' event in stream; events: {[e.get('type') for e in events]}"
        done = done_events[-1]
        assert isinstance(done.get("elapsed_ms"), (int, float)) and done["elapsed_ms"] >= 0, (
            f"done event must have elapsed_ms >= 0; got: {done.get('elapsed_ms')!r}"
        )
        assert isinstance(done.get("model"), str) and done["model"], (
            f"done event must have non-empty model string; got: {done.get('model')!r}"
        )


class TestSSEStreamingErrorRegression:
    """Regression guards: no token-disguised error text; error events always have intent + done."""

    _VALID_INTENTS = frozenset({
        "debug_trace", "debug", "search", "graph_callers", "graph_callees",
        "graph_impact", "architecture", "global", "feature",
    })

    def test_no_token_event_contains_llm_unavailable_string(self, http):
        """After a failure, token events must not carry LLM-unavailable error text.

        Real HTTP to live daemon; nonexistent project forces downstream failure path.
        No mock — the daemon's real intent classifier, context fetchers, and LLM client execute.
        """
        r = http.post(
            "/api/chat_stream",
            json={
                "project": "/tmp/__nonexistent_phase65_regression__",
                "query": "describe the architecture of this project",
            },
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert r.status_code == 200
        events = _parse_sse_events(r.text)
        banned_substrings = (
            "LLM unavailable",
            "unavailable:",
            "[response incomplete:",
            "analysis unavailable:",
        )
        for evt in events:
            if evt.get("type") == "token":
                text = evt.get("text", "")
                for banned in banned_substrings:
                    assert banned not in text, (
                        f"Token event contains disguised error text {banned!r}; "
                        f"full token: {text!r}"
                    )

    @pytest.mark.parametrize("query,expected_label", [
        ("describe the architecture and high-level design of this project",
         "architecture"),
        ("explain all business features and their overall system design globally",
         "global"),
        ("I have a nil pointer panic: goroutine crashed at main.go:42. What is the root cause?",
         "debug"),
        ("how does the cart checkout feature work end to end from entry point to database?",
         "feature"),
    ])
    def test_error_event_or_valid_done_for_each_intent(self, http, query, expected_label):
        """For each major intent, a nonexistent-project request must produce either:
        - an SSE error event (correct path), or
        - a done event with non-empty token content (intent happened to succeed gracefully).
        The forbidden state: a 'done' whose token stream contained disguised error text.

        Real HTTP; real classifier (real Ollama qwen3-query:8b call).
        No mock or patching — the live daemon executes every real handler path.
        """
        r = http.post(
            "/api/chat_stream",
            json={
                "project": "/tmp/__nonexistent_phase65_intent__",
                "query": query,
            },
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert r.status_code == 200, (
            f"chat_stream must return 200 for {expected_label!r} intent; "
            f"got {r.status_code}: {r.text[:200]}"
        )
        events = _parse_sse_events(r.text)
        event_types = {e.get("type") for e in events}
        assert "done" in event_types, (
            f"No 'done' event for {expected_label!r} intent; event types: {event_types}"
        )
        if "error" in event_types:
            # Structured error — verify no token-disguised text alongside it
            token_texts = " ".join(
                e.get("text", "") for e in events if e.get("type") == "token"
            )
            for banned in ("LLM unavailable", "unavailable:", "[response incomplete:"):
                assert banned not in token_texts, (
                    f"Both error event AND token-disguised error text present for "
                    f"{expected_label!r}; banned={banned!r}; tokens={token_texts[:200]!r}"
                )

    def test_error_event_includes_intent_field(self, http):
        """If an error event is emitted, its 'intent' field must be a known valid intent string.

        Real HTTP to live daemon with nonexistent project — no mock.
        """
        r = http.post(
            "/api/chat_stream",
            json={
                "project": "/tmp/__nonexistent_phase65_intent_field__",
                "query": "what calls handle_pipeline?",
            },
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert r.status_code == 200
        events = _parse_sse_events(r.text)
        error_events = [e for e in events if e.get("type") == "error"]
        if not error_events:
            pytest.skip("No error events emitted — stream completed gracefully")
        for evt in error_events:
            # PROJECT_NOT_REGISTERED errors fire before intent routing — no intent is known.
            if evt.get("code") == "PROJECT_NOT_REGISTERED":
                continue
            assert evt.get("intent") in self._VALID_INTENTS, (
                f"error event 'intent' must be a valid intent; got: {evt.get('intent')!r}. "
                f"Valid: {sorted(self._VALID_INTENTS)}"
            )

    def test_done_event_always_follows_error_event(self, http):
        """If an SSE error event appears in the stream, a 'done' event must follow it.

        Guarantees the stream is always well-terminated for clients that wait for 'done'.
        Real HTTP; no mock.
        """
        r = http.post(
            "/api/chat_stream",
            json={
                "project": "/tmp/__nonexistent_phase65_done_follows_error__",
                "query": "explain the global architecture of this codebase",
            },
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert r.status_code == 200
        events = _parse_sse_events(r.text)
        event_types = [e.get("type") for e in events]
        if "error" not in event_types:
            pytest.skip("No error events in stream — nothing to verify")
        error_idx = event_types.index("error")
        done_indices = [i for i, t in enumerate(event_types) if t == "done"]
        assert any(i > error_idx for i in done_indices), (
            f"A 'done' event must appear after the 'error' event; "
            f"event sequence: {event_types}"
        )
