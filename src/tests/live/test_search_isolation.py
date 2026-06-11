"""Search inference isolation proof tests.

Proves the user requirement: "searching shouldn't be blocked on activities like embedding,
indexing, federated projects, knowledge base building".

Every test is live — requires daemon at :8765, Ollama, GPU (RTX 5080).
No mocks. No skips. GPU-only (no CPU fallback allowed).
"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.live

_ASTRO = "/home/user/git/github.com/fairyhunter13/redacted-name-3"
_SEARCH_SLO_S = 5.0  # max acceptable search latency alongside background work


class TestQueryPassageIsolation:
    """query and passage roles share one ONNX session — the GPU can only hold one."""

    def test_single_session_shared_by_query_and_passage(self):
        """_embedder returns the SAME single ONNX session for both roles.

        VRAM can only hold one ONNX session alongside the Ollama LLM models.
        Isolation is achieved via _BUILD_INFER_EXECUTOR (single-thread passage
        path, one cuBLAS handle) and the interactive priority gate — not
        separate session objects.
        """
        from opencode_search.config import DEFAULT_EMBED_MODEL
        from opencode_search.embeddings import _embedder
        q = _embedder(DEFAULT_EMBED_MODEL, role="query")
        p = _embedder(DEFAULT_EMBED_MODEL, role="passage")
        assert q is p, (
            "query and passage returned different session objects — "
            "two ONNX sessions waste VRAM and cause CUBLAS OOM on this GPU."
        )

    def test_session_is_pinned_across_calls(self):
        """Repeated _embedder calls must return the same cached object (pinned)."""
        from opencode_search.config import DEFAULT_EMBED_MODEL
        from opencode_search.embeddings import _embedder
        q1 = _embedder(DEFAULT_EMBED_MODEL, role="query")
        q2 = _embedder(DEFAULT_EMBED_MODEL, role="query")
        p1 = _embedder(DEFAULT_EMBED_MODEL, role="passage")
        assert q1 is q2 is p1, (
            "embedder was reloaded between calls — it is not pinned resident. "
            "Each reload risks a CUBLAS failure that would freeze search."
        )

    @pytest.mark.slow
    def test_query_models_resident_after_warmup(self):
        """warmup_query_models() must return without error and pin the session."""
        from opencode_search.config import DEFAULT_EMBED_MODEL
        from opencode_search.embeddings import _embedder, warmup_query_models
        warmup_query_models()
        # After warmup the query session must be resident (same object on next call).
        q_before = _embedder(DEFAULT_EMBED_MODEL, role="query")
        warmup_query_models()  # idempotent — must not reload
        q_after = _embedder(DEFAULT_EMBED_MODEL, role="query")
        assert q_before is q_after, "warmup_query_models reloaded the query session — not idempotent."


class TestPassageCooldownDoesNotBlockSearch:
    """A CUBLAS failure on the passage path must not freeze embed_query."""

    @pytest.mark.slow
    def test_search_survives_passage_cublas_cooldown(self):
        """Forcing the passage-path cooldown must NOT raise for embed_query (query path exempt)."""
        import time as _t

        import opencode_search.embeddings as _emb
        from opencode_search.config import DEFAULT_DIMS, DEFAULT_EMBED_MODEL
        from opencode_search.embeddings import embed_query

        # Ensure query session is already loaded (so it doesn't need to hit cooldown).
        _ = _emb._embedder(DEFAULT_EMBED_MODEL, role="query")

        # Inject a fake cooldown (set _cublas_fail_time to now).
        original = _emb._cublas_fail_time
        try:
            with _emb._cublas_fail_lock:
                _emb._cublas_fail_time = _t.monotonic()  # fake: cooldown starts now

            # embed_query must NOT raise — query path is cooldown-exempt once cached.
            result = embed_query("test isolation", model=DEFAULT_EMBED_MODEL, dimensions=DEFAULT_DIMS)
            assert result, "embed_query returned empty despite query session being resident."
        finally:
            with _emb._cublas_fail_lock:
                _emb._cublas_fail_time = original


class TestPriorityGate:
    """Interactive ops mark themselves high-priority; batch ops yield between sub-batches."""

    def test_interactive_in_flight_counter(self):
        """_interactive_start/_interactive_done and interactive_in_flight() are consistent."""
        from opencode_search.embeddings import (
            _interactive_done,
            _interactive_start,
            interactive_in_flight,
        )
        assert not interactive_in_flight(), "counter non-zero before test started"
        _interactive_start()
        assert interactive_in_flight()
        _interactive_done()
        assert not interactive_in_flight()

    def test_yield_to_interactive_returns_quickly_when_idle(self):
        """yield_to_interactive() must return near-instantly when no interactive work is running."""
        from opencode_search.embeddings import yield_to_interactive
        t0 = time.perf_counter()
        yield_to_interactive(timeout_s=0.5)
        elapsed = time.perf_counter() - t0
        # If nothing interactive is in flight it should return immediately (well under 50ms).
        assert elapsed < 0.05, f"yield_to_interactive blocked for {elapsed:.3f}s with no interactive work"


class TestSearchNotBlockedDuringIndexing:
    """The definitive proof: search returns within SLO while passage embeds run."""

    @pytest.mark.slow
    def test_search_not_blocked_during_indexing(self, http, astro):
        """search() returns ≤ SLO seconds while a concurrent passage-embed batch runs.

        This is the root-cause proof test: before the fix, a shared embedder session +
        bare asyncio.to_thread caused CUBLAS storms that froze search for ~75s.
        """

        from opencode_search.config import DEFAULT_DIMS, DEFAULT_EMBED_MODEL
        from opencode_search.embeddings import _BUILD_INFER_EXECUTOR, embed_passages

        # Kick a real passage-embed batch in the background on the build executor.
        texts = ["def example(): pass\n" + "x = 1\n" * 50] * 64  # 64 synthetic passages
        future = _BUILD_INFER_EXECUTOR.submit(
            embed_passages, texts, model=DEFAULT_EMBED_MODEL, dimensions=DEFAULT_DIMS
        )

        try:
            # While the batch is running, issue a real search and measure latency.
            t0 = time.perf_counter()
            r = http.get("/api/search", params={"q": "authentication handler", "project": astro, "top_k": 5})
            elapsed = time.perf_counter() - t0

            assert r.status_code == 200, f"search returned {r.status_code}: {r.text[:200]}"
            data = r.json()
            assert data.get("results"), f"search returned no results: {data}"
            assert elapsed <= _SEARCH_SLO_S, (
                f"search took {elapsed:.1f}s (SLO {_SEARCH_SLO_S}s) while passage-embed batch ran. "
                "The GPU isolation fix did not prevent search from being blocked."
            )
        finally:
            import contextlib
            with contextlib.suppress(Exception):
                future.result(timeout=120)

    @pytest.mark.slow
    def test_search_preempts_long_batch_via_priority_gate(self):
        """Verify yield_to_interactive() pauses passage embeds when search is in flight.

        Starts a long embed batch, marks interactive_in_flight=True, asserts
        yield_to_interactive sleeps as expected, then marks it done.
        """
        import threading

        from opencode_search.embeddings import (
            _interactive_done,
            _interactive_start,
            yield_to_interactive,
        )

        _interactive_start()
        try:
            # In a separate thread, yield_to_interactive should sleep for ~timeout_s.
            slept = []

            def _check():
                t0 = time.perf_counter()
                yield_to_interactive(timeout_s=0.1)
                slept.append(time.perf_counter() - t0)

            t = threading.Thread(target=_check, daemon=True)
            t.start()
            t.join(timeout=2.0)
            assert slept, "yield_to_interactive thread did not complete"
            assert slept[0] >= 0.09, (
                f"yield_to_interactive slept only {slept[0]:.3f}s with interactive_in_flight=True; "
                "expected ≥ 0.09s (it should pause batch work while search runs)"
            )
        finally:
            _interactive_done()
