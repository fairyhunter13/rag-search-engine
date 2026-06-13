"""Performance SLO tests for opencode-search-engine.

Verifies that each intent/endpoint stays within generous upper bounds.
These are regression guards, not benchmarks. Thresholds are set well above
typical observed latencies — a violation means something has severely degraded.

All chat_stream tests extract `elapsed_ms` from the SSE done event that the
backend already includes. No new timing infrastructure is needed.

SLO thresholds:
  search intent        < 12 s
  graph callers        < 25 s
  feature intent       < 60 s
  architecture intent  < 90 s
  debug_trace intent   < 60 s
  global intent        < 150 s  (MAP-REDUCE across 5900+ communities)
  GET /api/search      < 5 s
  GET /healthz         < 1 s
"""
from __future__ import annotations

import time

import pytest

from .conftest import parse_sse
from .test_astro_e2e import _ASTRO, _chat

pytestmark = pytest.mark.live

_ASTRO_PATH = _ASTRO


def _await_gpu_cool(threshold_c: int = 72, timeout_s: float = 240.0, poll_s: float = 10.0) -> int | None:
    """Wait until the GPU is at/below threshold_c, so latency SLOs are measured
    under nominal (non-thermally-throttled) conditions rather than while the GPU
    is heat-soaked by the preceding LLM-heavy tests.

    A real user issues a global query against a cool/idle GPU; the daemon's 85°C
    thermal guard pauses inference mid-synthesis when the GPU is hot, which is not
    the condition the SLO describes. No-op (returns None) if nvidia-smi is absent.
    Bounded by timeout_s. Returns the last observed temperature.
    """
    import shutil
    import subprocess

    if shutil.which("nvidia-smi") is None:
        return None
    deadline = time.monotonic() + timeout_s
    temp: int | None = None
    while time.monotonic() < deadline:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip().splitlines()
            temp = int(out[0]) if out and out[0].strip().isdigit() else None
        except Exception:
            return temp
        if temp is None or temp <= threshold_c:
            return temp
        time.sleep(poll_s)
    return temp



# ---------------------------------------------------------------------------
# Class 1: chat_stream SLOs (extract elapsed_ms from done event)
# ---------------------------------------------------------------------------

class TestChatStreamSLOs:
    """Each chat intent must respond within its SLO threshold."""

    pytestmark = pytest.mark.slow

    def test_search_intent_slo(self, http, astro):
        """search intent (vector lookup) must complete in < 12s."""
        _, intent, _, elapsed, _ = _chat(http, astro,
            "find the main gRPC service definition files")
        assert intent == "search", f"Expected search intent; got {intent!r}"
        assert elapsed < 12_000, f"search SLO violated: {elapsed}ms >= 12000ms"

    def test_graph_callers_intent_slo(self, http, astro):
        """graph_callers (graph traversal) must complete in < 25s."""
        _, intent, _, elapsed, _ = _chat(http, astro,
            "what calls the AddToCart gRPC method?")
        assert intent in ("graph_callers", "search", "feature"), (
            f"Unexpected intent: {intent!r}"
        )
        assert elapsed < 25_000, f"graph SLO violated: {elapsed}ms >= 25000ms"

    def test_feature_intent_slo(self, http, astro):
        """feature intent (context assembly + LLM) must complete in < 60s."""
        _, intent, _, elapsed, _ = _chat(http, astro,
            "how does the cart checkout feature work end to end?")
        assert intent in ("feature", "global"), f"Expected feature; got {intent!r}"
        assert elapsed < 60_000, f"feature SLO violated: {elapsed}ms >= 60000ms"

    def test_architecture_intent_slo(self, http, astro):
        """architecture intent must complete in < 90s."""
        _, intent, _, elapsed, _ = _chat(http, astro,
            "what is the overall system architecture?")
        assert intent in ("architecture", "global"), (
            f"Expected architecture; got {intent!r}"
        )
        assert elapsed < 90_000, f"architecture SLO violated: {elapsed}ms >= 90000ms"

    def test_debug_trace_intent_slo(self, http, astro):
        """debug_trace intent must complete in < 60s."""
        _, intent, _, elapsed, _ = _chat(http, astro,
            "goroutine 10 [running]:\npanic: nil pointer dereference")
        assert intent in ("debug_trace", "debug"), (
            f"Expected debug_trace; got {intent!r}"
        )
        assert elapsed < 60_000, f"debug_trace SLO violated: {elapsed}ms >= 60000ms"

    def test_global_intent_slo(self, http, astro):
        """global intent (MAP-REDUCE synthesis) must complete in < 150s.

        Measured under nominal conditions: wait for the GPU to leave the
        thermally-throttled state first (the preceding LLM-heavy slow tests can
        heat-soak a laptop GPU past the daemon's 85°C guard, which would pause
        inference mid-synthesis — not representative of a real global query).
        """
        _await_gpu_cool()
        _, intent, _, elapsed, _ = _chat(http, astro,
            "give me a comprehensive global overview of the entire astro platform")
        assert intent == "global", f"Expected global; got {intent!r}"
        assert elapsed < 150_000, f"global SLO violated: {elapsed}ms >= 150000ms"

    def test_elapsed_ms_always_present_in_done_event(self, http, project):
        """chat_stream done event must always include elapsed_ms (regression guard)."""
        r = http.post(
            "/api/chat_stream",
            json={"project": project, "query": "what files exist?"},
            headers={"Accept": "text/event-stream"},
        )
        assert r.status_code == 200
        events = parse_sse(r)
        done = next((e for e in events if e.get("type") == "done"), {})
        assert "elapsed_ms" in done, (
            f"done event missing elapsed_ms field; keys: {list(done.keys())}"
        )
        assert isinstance(done["elapsed_ms"], int), (
            f"elapsed_ms must be int; got {type(done['elapsed_ms'])}"
        )
        assert done["elapsed_ms"] > 0, f"elapsed_ms must be positive; got {done['elapsed_ms']}"


# ---------------------------------------------------------------------------
# Class 2: HTTP API endpoint SLOs (fast, no LLM)
# ---------------------------------------------------------------------------

class TestAPIEndpointSLOs:
    """Core HTTP endpoints must respond within SLO thresholds without LLM calls."""

    def test_search_api_slo(self, http, project):
        """GET /api/search must return within 5s (vector lookup, no LLM)."""
        t0 = time.perf_counter()
        r = http.get("/api/search", params={"q": "handler", "project": project})
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert r.status_code == 200
        api_elapsed = r.json().get("elapsed_ms", elapsed_ms)
        assert api_elapsed < 5_000, (
            f"search API SLO violated: {api_elapsed:.0f}ms >= 5000ms"
        )

    def test_healthz_slo(self, http):
        """GET /healthz must respond in < 1s (no I/O)."""
        t0 = time.perf_counter()
        r = http.get("/healthz")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert r.status_code == 200
        assert elapsed_ms < 1_000, (
            f"healthz SLO violated: {elapsed_ms:.0f}ms >= 1000ms"
        )

    def test_metrics_p50_less_than_p95(self, http):
        """p50 must be ≤ p95 — sanity check on the metrics math."""
        r = http.get("/api/metrics")
        assert r.status_code == 200
        lats = r.json().get("latency_ms", {})
        p50, p95 = lats.get("p50"), lats.get("p95")
        if p50 and p95:
            assert p50 <= p95, (
                f"Metrics math broken: p50 ({p50}) > p95 ({p95})"
            )
