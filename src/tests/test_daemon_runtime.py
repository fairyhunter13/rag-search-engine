"""Tests for daemon runtime state and idle shutdown logic."""
from __future__ import annotations

from opencode_search.daemon_runtime import _RuntimeState


def test_runtime_state_tracks_clients_and_activity():
    state = _RuntimeState()

    state.client_open("client-a", project_path="/tmp/proj")
    snapshot = state.snapshot()

    assert snapshot["active_clients"] == 1
    assert snapshot["client_ids"] == ["client-a"]
    assert snapshot["active_projects"] == ["/tmp/proj"]

    closed = state.client_close("client-a")
    assert closed == "/tmp/proj"
    assert state.snapshot()["active_clients"] == 1
    assert state.snapshot()["closing_clients"] == ["client-a"]


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


def test_runtime_state_counts_clients_per_project():
    state = _RuntimeState()

    state.client_open("client-a", project_path="/tmp/proj")
    state.client_open("client-b", project_path="/tmp/proj")
    state.client_open("client-c", project_path="/tmp/other")

    assert state.project_client_count("/tmp/proj") == 2
    assert state.project_client_count("/tmp/other") == 1

    state.client_close("client-a")
    assert state.project_client_count("/tmp/proj") == 2


def test_runtime_state_releases_projects_only_after_stale_disconnect():
    state = _RuntimeState()

    state.client_open("client-a", project_path="/tmp/proj")
    state.client_close("client-a")
    assert state.project_client_count("/tmp/proj") == 1

    state.active_clients["client-a"] -= 120
    released = state.releaseable_stale_projects(60)

    assert released == ["/tmp/proj"]
    assert state.project_client_count("/tmp/proj") == 0


def test_runtime_state_binds_open_client_to_project_after_index():
    state = _RuntimeState()

    state.client_open("client-a", cwd="/tmp/proj/subdir")

    bound = state.bind_clients_to_project("/tmp/proj")

    assert bound == 1
    assert state.project_client_count("/tmp/proj") == 1


def test_runtime_state_does_not_overwrite_existing_child_project_binding():
    state = _RuntimeState()

    state.client_open("client-a", cwd="/tmp/repo/subproj/src", project_path="/tmp/repo/subproj")

    bound = state.bind_clients_to_project("/tmp/repo")

    assert bound == 0
    assert state.project_client_count("/tmp/repo/subproj") == 1
    assert state.project_client_count("/tmp/repo") == 0


def test_runtime_state_does_not_rebind_clients_that_are_closing():
    state = _RuntimeState()

    state.client_open("client-a", cwd="/tmp/proj/subdir")
    state.client_close("client-a")

    bound = state.bind_clients_to_project("/tmp/proj")

    assert bound == 0
    assert state.project_client_count("/tmp/proj") == 0
    assert state.snapshot()["closing_clients"] == ["client-a"]
