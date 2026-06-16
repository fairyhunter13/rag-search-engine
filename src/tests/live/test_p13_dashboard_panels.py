"""P13.4 — Dashboard metrics endpoint E2E (no mocks, real daemon)."""
from __future__ import annotations

import pytest


@pytest.mark.live
def test_metrics_endpoint(live_client):
    """metrics returns search, chat_stream, and rerank counters (D3: rerank lift metric)."""
    resp = live_client.get("/api/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert "search" in data
    assert "chat_stream" in data
    assert "rerank" in data, f"rerank block missing: {data}"
    assert "queries" in data["rerank"], f"rerank.queries missing: {data['rerank']}"
    assert "top1_changed" in data["rerank"], f"rerank.top1_changed missing: {data['rerank']}"
