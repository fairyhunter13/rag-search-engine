"""Tests for daemon runtime state and idle shutdown logic."""
from __future__ import annotations

from opencode_search.daemon_runtime import _RuntimeState


def test_runtime_state_tracks_clients_and_activity():
    state = _RuntimeState()

    state.client_open("client-a")
    snapshot = state.snapshot()

    assert snapshot["active_clients"] == 1
    assert snapshot["client_ids"] == ["client-a"]

    state.client_close("client-a")
    assert state.snapshot()["active_clients"] == 0


def test_runtime_state_prunes_stale_clients():
    state = _RuntimeState()
    state.client_open("client-a")
    state.active_clients["client-a"] -= 120

    pruned = state.prune_stale_clients(60)

    assert pruned == 1
    assert state.snapshot()["active_clients"] == 0


def test_runtime_state_should_shutdown_only_when_idle_and_no_clients():
    state = _RuntimeState()
    state.client_open("client-a")
    state.last_activity_monotonic -= 3600

    assert state.should_shutdown(idle_timeout_s=300, stale_after_s=60) is False

    state.active_clients["client-a"] -= 120

    assert state.should_shutdown(idle_timeout_s=300, stale_after_s=60) is True

