"""Tests for opencode_search.metrics."""
from __future__ import annotations

import time
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


# ---------------------------------------------------------------------------
# SQLite persistence round-trip tests (record_search_event → /api/metrics/history)
# ---------------------------------------------------------------------------

def test_record_search_event_writes_to_sqlite(tmp_path, monkeypatch):
    """record_search_event() inserts a row into the SQLite metrics DB."""
    import opencode_search.dashboard as dash

    # Redirect SQLite DB to a tmp dir so we don't pollute real DB
    monkeypatch.setattr(dash, "_METRICS_DB", tmp_path / "metrics_test.db")
    monkeypatch.setattr(dash, "_DATA_DIR", tmp_path)

    dash.record_search_event(
        query="test query",
        scope="code",
        result_count=5,
        top_score=0.95,
        latency_ms=42.0,
        project="/tmp/test_project",
    )

    conn = dash._get_metrics_db()
    rows = conn.execute("SELECT * FROM search_events").fetchall()
    conn.close()

    assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
    row = rows[0]
    assert row["query"] == "test query"
    assert row["result_count"] == 5
    assert abs(row["latency_ms"] - 42.0) < 0.01
    assert abs(row["top_score"] - 0.95) < 0.01


def test_record_multiple_search_events_accumulate(tmp_path, monkeypatch):
    """Multiple record_search_event calls accumulate rows in SQLite."""
    import opencode_search.dashboard as dash

    monkeypatch.setattr(dash, "_METRICS_DB", tmp_path / "metrics_multi.db")
    monkeypatch.setattr(dash, "_DATA_DIR", tmp_path)

    for i in range(5):
        dash.record_search_event(
            query=f"query_{i}",
            scope="all",
            result_count=i,
            top_score=0.5,
            latency_ms=10.0 * (i + 1),
            project="/tmp/proj",
        )

    conn = dash._get_metrics_db()
    count = conn.execute("SELECT COUNT(*) FROM search_events").fetchone()[0]
    conn.close()

    assert count == 5, f"Expected 5 rows, got {count}"


def test_record_search_event_ts_is_recent(tmp_path, monkeypatch):
    """Recorded event has a timestamp within the last 5 seconds."""
    import opencode_search.dashboard as dash

    monkeypatch.setattr(dash, "_METRICS_DB", tmp_path / "metrics_ts.db")
    monkeypatch.setattr(dash, "_DATA_DIR", tmp_path)

    before = time.time()
    dash.record_search_event("q", "code", 1, 0.8, 30.0, "/p")
    after = time.time()

    conn = dash._get_metrics_db()
    row = conn.execute("SELECT ts FROM search_events").fetchone()
    conn.close()

    assert before <= row["ts"] <= after + 0.1, \
        f"Timestamp {row['ts']} not in expected range [{before}, {after}]"
