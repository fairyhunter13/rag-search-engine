"""P13.4 — Dashboard integration panels E2E (no mocks, real daemon)."""
from __future__ import annotations

import pytest


@pytest.mark.live
def test_integrations_status_all_clients(live_client):
    """integrations_status returns all managed targets, all in sync."""
    resp = live_client.get("/api/integrations_status")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is True, f"not all in sync: {data}"
    clients = data.get("clients", [])
    assert data.get("total") == 18
    assert len(clients) == 18
    statuses = {c["status"] for c in clients}
    assert statuses <= {"already_ok", "configured", "skipped"}, f"unexpected statuses: {statuses}"
    tools = {c["tool"] for c in clients}
    assert "claude(.claude)/CLAUDE.md" in tools
    assert "codex/AGENTS.md" in tools
    assert "hermes/config.yaml" in tools
    assert "bash_aliases" in tools


@pytest.mark.live
def test_integrations_status_hermes_codex_opencode_present(live_client):
    """All 7 client families appear in integrations_status."""
    resp = live_client.get("/api/integrations_status")
    assert resp.status_code == 200
    clients = resp.json().get("clients", [])
    tool_str = " ".join(c["tool"] for c in clients)
    for expected in ("codex", "hermes", "opencode-default", "opencode-personal",
                     ".claude)", ".claude-account1", ".claude-account2"):
        assert expected in tool_str, f"missing family '{expected}' in tool list"


@pytest.mark.live
def test_system_status_live_gpu(live_client):
    """system_status returns live GPU data and uptime > 0."""
    resp = live_client.get("/api/system_status")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is True
    assert data.get("uptime_s", 0) > 0
    assert data.get("gpu_temp_c", 0) > 0, "GPU temp should be non-zero (GPU is required)"
    assert data.get("vram_free_mb", 0) > 0


@pytest.mark.live
def test_metrics_endpoint(live_client):
    """metrics endpoint returns a dict with search and chat_stream counters."""
    resp = live_client.get("/api/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert "search" in data
    assert "chat_stream" in data
