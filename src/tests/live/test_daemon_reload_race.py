"""Tests for daemon reload race-condition fix.

All tests require the live daemon under systemd management and exercise real
socket bind/release behaviour — no mocks.
"""
from __future__ import annotations

import subprocess
import time

import pytest

pytestmark = pytest.mark.live

DAEMON_URL = "http://localhost:8765"
_RECOVER_TIMEOUT = 12.0


def _wait_healthy(http, timeout: float = _RECOVER_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = http.get("/healthz", timeout=3.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _current_pid(http) -> int | None:
    try:
        r = http.get("/api/system_status", timeout=5.0)
        return r.json().get("pid")
    except Exception:
        return None


def _systemd_is_active() -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "opencode-search-mcp-daemon"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() == "active"


def test_mcp_manage_reload_uses_shared_helper():
    """mcp.py manage(action=reload) must delegate to _spawn_daemon_restart_thread (B2 audit)."""
    import inspect  # noqa: I001
    from opencode_search import mcp as mcp_mod
    from opencode_search.dashboard import _spawn_daemon_restart_thread  # noqa: F401
    src = inspect.getsource(mcp_mod)
    assert "_spawn_daemon_restart_thread" in src, (
        "mcp.py manage handler must call _spawn_daemon_restart_thread — unified reload path"
    )
    assert "signal.SIGTERM" not in src.split("def manage(")[1].split("\ndef ")[0], (
        "mcp.py manage handler must NOT inline its own SIGTERM logic anymore"
    )


@pytest.mark.flaky(reruns=2)
def test_reload_via_api_reload_endpoint(http):
    """POST /api/reload must restart daemon; new PID must differ and /healthz must recover."""
    old_pid = _current_pid(http)
    r = http.post("/api/reload", timeout=10.0)
    assert r.status_code == 200
    assert r.json().get("status") == "reloading"

    time.sleep(0.5)
    assert _wait_healthy(http), f"Daemon did not recover within {_RECOVER_TIMEOUT}s after /api/reload"

    new_pid = _current_pid(http)
    if old_pid is not None and new_pid is not None:
        assert new_pid != old_pid, "Expected a new PID after /api/reload"


@pytest.mark.flaky(reruns=2)
def test_rapid_back_to_back_reloads_dont_fail_unit(http):
    """Three reloads spaced 2s apart must not push systemd into 'failed' state."""
    subprocess.run(
        ["systemctl", "--user", "reset-failed", "opencode-search-mcp-daemon"],
        capture_output=True,
    )
    assert _wait_healthy(http, timeout=8.0), "Daemon must be healthy before rapid-reload test"

    import contextlib
    for _ in range(3):
        with contextlib.suppress(Exception):
            http.post("/api/reload", timeout=5.0)
        time.sleep(2.0)

    assert _wait_healthy(http, timeout=15.0), (
        "Daemon did not recover within 15s after 3 rapid reloads"
    )
    assert _systemd_is_active(), "systemd unit is not 'active' after rapid reloads — hit burst limit"


def test_port_release_wait_returns_true_when_free():
    """_wait_for_port_free must return True quickly for a port with no listener."""
    from opencode_search.daemon import _wait_for_port_free
    # Port 8766 is not used by the daemon (8765 is)
    result = _wait_for_port_free("127.0.0.1", 8766, timeout_s=2.0)
    assert result is True, "_wait_for_port_free should return True for a free port"
