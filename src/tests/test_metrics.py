"""Tests for opencode_search.metrics."""
from __future__ import annotations

import pytest

from opencode_search.metrics import get_metrics, record_search, reset_metrics


@pytest.fixture(autouse=True)
def clean_metrics():
    reset_metrics()
    yield
    reset_metrics()


def test_initial_state_is_zero():
    m = get_metrics()
    assert m["call_count"] == 0
    assert m["zero_result_count"] == 0
    assert m["zero_result_rate"] == 0.0
    assert m["latency_ms"]["avg"] == 0.0
    assert m["latency_ms"]["min"] is None
    assert m["latency_ms"]["max"] is None
    assert m["latency_ms"]["p50"] is None
    assert m["latency_ms"]["p95"] is None
    assert m["avg_top_score"] is None


def test_single_record_with_results():
    record_search(100.0, result_count=5, top_score=0.95)
    m = get_metrics()
    assert m["call_count"] == 1
    assert m["zero_result_count"] == 0
    assert m["zero_result_rate"] == 0.0
    assert m["latency_ms"]["avg"] == 100.0
    assert m["latency_ms"]["min"] == 100.0
    assert m["latency_ms"]["max"] == 100.0
    assert m["avg_top_score"] == pytest.approx(0.95, abs=1e-4)


def test_zero_result_count():
    record_search(50.0, result_count=0, top_score=None)
    record_search(60.0, result_count=3, top_score=0.8)
    m = get_metrics()
    assert m["call_count"] == 2
    assert m["zero_result_count"] == 1
    assert m["zero_result_rate"] == pytest.approx(0.5, abs=1e-4)


def test_latency_min_max_avg():
    record_search(10.0, 1, 0.5)
    record_search(20.0, 1, 0.6)
    record_search(30.0, 1, 0.7)
    m = get_metrics()
    assert m["latency_ms"]["min"] == 10.0
    assert m["latency_ms"]["max"] == 30.0
    assert m["latency_ms"]["avg"] == pytest.approx(20.0, abs=0.1)


def test_latency_percentiles():
    for i in range(1, 11):
        record_search(float(i * 10), 1, None)
    m = get_metrics()
    # 10 samples: [10,20,30,40,50,60,70,80,90,100]
    # p50 index = 10//2 = 5 → 60
    # p95 index = min(int(10*0.95), 9) = min(9,9) = 9 → 100
    assert m["latency_ms"]["p50"] == pytest.approx(60.0, abs=0.1)
    assert m["latency_ms"]["p95"] == pytest.approx(100.0, abs=0.1)


def test_top_score_none_skipped():
    record_search(10.0, 0, None)
    m = get_metrics()
    assert m["avg_top_score"] is None


def test_top_score_average_multiple():
    record_search(10.0, 2, 0.8)
    record_search(20.0, 3, 0.6)
    m = get_metrics()
    assert m["avg_top_score"] == pytest.approx(0.7, abs=1e-4)


def test_reset_clears_all():
    record_search(100.0, 5, 0.9)
    reset_metrics()
    m = get_metrics()
    assert m["call_count"] == 0
    assert m["zero_result_count"] == 0
    assert m["avg_top_score"] is None
    assert m["latency_ms"]["min"] is None


def test_multiple_records_accumulate():
    for _ in range(5):
        record_search(200.0, 2, 0.75)
    m = get_metrics()
    assert m["call_count"] == 5
    assert m["latency_ms"]["avg"] == pytest.approx(200.0, abs=0.1)
    assert m["avg_top_score"] == pytest.approx(0.75, abs=1e-4)


def test_thread_safety():
    """Concurrent record calls must not corrupt the counter."""
    import threading

    def worker():
        for _ in range(100):
            record_search(10.0, 1, 0.5)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    m = get_metrics()
    assert m["call_count"] == 1000
