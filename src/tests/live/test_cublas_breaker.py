"""Phase 68 Part C: CUBLAS OOM circuit breaker with adaptive retry + Ollama probe.

All tests exercise REAL code paths with real Python exceptions — no mocks.
The "CUBLAS error" is a real RuntimeError raised from a real callable.
The retry helper (the system under test) handles it with a real state machine.
Env knobs override timing constants so tests complete in <5s each.
"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.live


class TestCublasBreakerStateMachine:
    """Core breaker state: record_failure sets cooldown; cooldown clears after timeout."""

    def test_record_failure_sets_cooldown(self, monkeypatch):
        """_record_cublas_failure() must enter cooldown; after timeout it must clear."""
        import opencode_search.embeddings as emb

        monkeypatch.setattr(emb, "_CUBLAS_COOLDOWN_S", 0.5)
        # reset state
        monkeypatch.setattr(emb, "_cublas_fail_time", 0.0)

        assert not emb._cublas_in_cooldown(), "should not be in cooldown at start"
        emb._record_cublas_failure()
        assert emb._cublas_in_cooldown(), "should be in cooldown immediately after failure"

        time.sleep(0.7)
        assert not emb._cublas_in_cooldown(), "cooldown should have expired after timeout"

    def test_record_failure_increments_hard_cooldowns_entered(self, monkeypatch):
        """Each _record_cublas_failure() call must increment the hard_cooldowns_entered counter."""
        import opencode_search.embeddings as emb

        monkeypatch.setattr(emb, "_CUBLAS_COOLDOWN_S", 0.1)
        monkeypatch.setattr(emb, "_cublas_fail_time", 0.0)
        before = emb._cublas_hard_cooldowns_entered

        emb._record_cublas_failure()
        assert emb._cublas_hard_cooldowns_entered == before + 1, (
            f"hard_cooldowns_entered not incremented: before={before}, "
            f"after={emb._cublas_hard_cooldowns_entered}"
        )


class TestCublasBreakerRetry:
    """_cublas_call_with_retry: non-CUBLAS errors pass through; CUBLAS errors retry then recover or enter cooldown."""

    def test_non_cublas_error_propagates_immediately(self, monkeypatch):
        """A ValueError (not CUBLAS) must propagate on first attempt without incrementing retry counter."""
        import opencode_search.embeddings as emb

        monkeypatch.setattr(emb, "_CUBLAS_BACKOFF_BASE_S", 0.01)
        monkeypatch.setattr(emb, "_CUBLAS_MAX_RETRIES", 3)
        monkeypatch.setattr(emb, "_cublas_fail_time", 0.0)
        monkeypatch.setattr(emb, "_CUBLAS_COOLDOWN_S", 60.0)
        before = emb._cublas_retry_attempts

        def bad():
            raise ValueError("not a cublas error")

        with pytest.raises(ValueError, match="not a cublas error"):
            emb._cublas_call_with_retry("test", bad)

        # retry counter must NOT be bumped for non-CUBLAS errors
        assert emb._cublas_retry_attempts == before, (
            "retry_attempts was incremented for a non-CUBLAS error"
        )

    @pytest.mark.flaky(reruns=2)
    def test_retry_recovers_when_call_eventually_succeeds(self, monkeypatch):
        """First call raises real CUBLAS RuntimeError; second returns 'ok' — must recover."""
        import opencode_search.embeddings as emb

        monkeypatch.setattr(emb, "_CUBLAS_BACKOFF_BASE_S", 0.05)
        monkeypatch.setattr(emb, "_CUBLAS_MAX_RETRIES", 3)
        monkeypatch.setattr(emb, "_cublas_fail_time", 0.0)
        monkeypatch.setattr(emb, "_CUBLAS_COOLDOWN_S", 60.0)
        monkeypatch.setattr(emb, "_cublas_retry_attempts", 0)
        monkeypatch.setattr(emb, "_cublas_retry_recoveries", 0)
        monkeypatch.setattr(emb, "_cublas_hard_cooldowns_entered", 0)

        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("CUBLAS_STATUS_ALLOC_FAILED error 3")
            return "ok"

        result = emb._cublas_call_with_retry("test", flaky)
        assert result == "ok", f"Expected 'ok', got {result!r}"
        assert emb._cublas_retry_attempts >= 1, "retry_attempts was not incremented"
        assert emb._cublas_retry_recoveries == 1, (
            f"retry_recoveries should be 1, got {emb._cublas_retry_recoveries}"
        )
        assert emb._cublas_hard_cooldowns_entered == 0, (
            "hard_cooldowns_entered must be 0 when call eventually succeeds"
        )

    @pytest.mark.flaky(reruns=2)
    def test_retry_enters_cooldown_after_max_attempts(self, monkeypatch):
        """If all retries fail with CUBLAS error, must enter hard cooldown and re-raise."""
        import opencode_search.embeddings as emb

        monkeypatch.setattr(emb, "_CUBLAS_BACKOFF_BASE_S", 0.05)
        monkeypatch.setattr(emb, "_CUBLAS_MAX_RETRIES", 2)
        monkeypatch.setattr(emb, "_cublas_fail_time", 0.0)
        monkeypatch.setattr(emb, "_CUBLAS_COOLDOWN_S", 60.0)
        monkeypatch.setattr(emb, "_cublas_retry_attempts", 0)
        monkeypatch.setattr(emb, "_cublas_retry_recoveries", 0)
        monkeypatch.setattr(emb, "_cublas_hard_cooldowns_entered", 0)

        def always_fails():
            raise RuntimeError("CUBLAS_STATUS_ALLOC_FAILED error 3")

        with pytest.raises(RuntimeError, match="CUBLAS"):
            emb._cublas_call_with_retry("test", always_fails)

        assert emb._cublas_in_cooldown(), "must be in hard cooldown after max retries exhausted"
        assert emb._cublas_hard_cooldowns_entered >= 1, (
            "hard_cooldowns_entered must be incremented after cooldown entry"
        )


class TestCublasOllamaProbe:
    """_probe_ollama_loading must hit real Ollama /api/ps and return a bool."""

    def test_ollama_probe_real_endpoint(self):
        """_probe_ollama_loading(timeout_s=2.0) must return bool without exception.

        Requires live Ollama at localhost:11434 (enforced by live marker contract).
        """
        from opencode_search.embeddings import _probe_ollama_loading
        result = _probe_ollama_loading(timeout_s=2.0)
        assert isinstance(result, bool), (
            f"_probe_ollama_loading must return bool; got {type(result).__name__}: {result!r}"
        )


class TestGetCublasMetrics:
    """get_cublas_metrics() must return a dict with all required keys."""

    def test_metrics_snapshot_structure(self):
        """get_cublas_metrics() must return all 6 expected keys with correct types."""
        from opencode_search.embeddings import get_cublas_metrics
        snap = get_cublas_metrics()
        assert isinstance(snap, dict), f"Expected dict, got {type(snap).__name__}"

        expected = {
            "retry_attempts": int,
            "retry_recoveries": int,
            "hard_cooldowns_entered": int,
            "ollama_waits": int,
            "in_cooldown": bool,
            "cooldown_remaining_s": float,
        }
        for key, typ in expected.items():
            assert key in snap, f"Missing key '{key}' in get_cublas_metrics() output: {snap}"
            assert isinstance(snap[key], typ), (
                f"Key '{key}' should be {typ.__name__}, got {type(snap[key]).__name__}: {snap[key]!r}"
            )
