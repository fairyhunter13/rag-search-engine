"""Thread-safe in-process metrics for opencode-search daemon search calls."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict

_MAX_SAMPLES = 1000


@dataclass
class _SearchMetrics:
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    call_count: int = 0
    zero_result_count: int = 0
    total_elapsed_ms: float = 0.0
    min_elapsed_ms: float = float("inf")
    max_elapsed_ms: float = 0.0
    _latencies: list[float] = field(default_factory=list, repr=False)
    _top_scores: list[float] = field(default_factory=list, repr=False)

    def record(self, elapsed_ms: float, result_count: int, top_score: float | None) -> None:
        with self._lock:
            self.call_count += 1
            self.total_elapsed_ms += elapsed_ms
            self.min_elapsed_ms = min(self.min_elapsed_ms, elapsed_ms)
            self.max_elapsed_ms = max(self.max_elapsed_ms, elapsed_ms)
            if len(self._latencies) < _MAX_SAMPLES:
                self._latencies.append(elapsed_ms)
            if result_count == 0:
                self.zero_result_count += 1
            if top_score is not None and len(self._top_scores) < _MAX_SAMPLES:
                self._top_scores.append(top_score)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            n = self.call_count
            avg_ms = self.total_elapsed_ms / n if n else 0.0
            p50 = p95 = None
            if self._latencies:
                s = sorted(self._latencies)
                k = len(s)
                p50 = s[k // 2]
                p95 = s[min(int(k * 0.95), k - 1)]
            avg_score = (
                sum(self._top_scores) / len(self._top_scores) if self._top_scores else None
            )
            return {
                "call_count": n,
                "zero_result_count": self.zero_result_count,
                "zero_result_rate": round(self.zero_result_count / n, 4) if n else 0.0,
                "latency_ms": {
                    "avg": round(avg_ms, 1),
                    "min": round(self.min_elapsed_ms, 1) if n else None,
                    "max": round(self.max_elapsed_ms, 1) if n else None,
                    "p50": round(p50, 1) if p50 is not None else None,
                    "p95": round(p95, 1) if p95 is not None else None,
                },
                "avg_top_score": round(avg_score, 4) if avg_score is not None else None,
            }

    def reset(self) -> None:
        with self._lock:
            self.call_count = 0
            self.zero_result_count = 0
            self.total_elapsed_ms = 0.0
            self.min_elapsed_ms = float("inf")
            self.max_elapsed_ms = 0.0
            self._latencies.clear()
            self._top_scores.clear()


_metrics = _SearchMetrics()


def record_search(elapsed_ms: float, result_count: int, top_score: float | None) -> None:
    """Record one search_code call. Safe to call from any thread."""
    _metrics.record(elapsed_ms, result_count, top_score)


def get_metrics() -> dict[str, Any]:
    """Return a point-in-time snapshot of accumulated search metrics."""
    return _metrics.snapshot()


def reset_metrics() -> None:
    """Reset all counters. Intended for tests."""
    _metrics.reset()


# ── Chat stream metrics ────────────────────────────────────────────────────────


@dataclass
class _ChatStreamMetrics:
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    stream_error_count: int = 0
    stream_success_count: int = 0
    _error_by_intent: Dict[str, int] = field(default_factory=dict, repr=False)

    def record_error(self, intent: str) -> None:
        with self._lock:
            self.stream_error_count += 1
            self._error_by_intent[intent] = self._error_by_intent.get(intent, 0) + 1

    def record_success(self) -> None:
        with self._lock:
            self.stream_success_count += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total = self.stream_error_count + self.stream_success_count
            return {
                "stream_error_count": self.stream_error_count,
                "stream_success_count": self.stream_success_count,
                "error_rate": round(self.stream_error_count / total, 4) if total else 0.0,
                "error_by_intent": dict(self._error_by_intent),
            }

    def reset(self) -> None:
        with self._lock:
            self.stream_error_count = 0
            self.stream_success_count = 0
            self._error_by_intent.clear()


_chat_metrics = _ChatStreamMetrics()


def record_stream_error(intent: str) -> None:
    """Record a streaming LLM failure for the given intent. Safe to call from any thread."""
    _chat_metrics.record_error(intent)


def record_stream_success() -> None:
    """Record a successful streaming LLM completion. Safe to call from any thread."""
    _chat_metrics.record_success()


def get_stream_metrics() -> dict[str, Any]:
    """Return a point-in-time snapshot of chat stream error metrics."""
    return _chat_metrics.snapshot()
