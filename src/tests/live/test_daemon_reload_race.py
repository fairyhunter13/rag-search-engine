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


def test_reload_uses_shared_helper_in_dashboard():
    """Reload path must delegate to _spawn_daemon_restart_thread in dashboard.py (B2 audit).

    Phase 100: manage() removed from MCP; reload now lives only in dashboard /api/reload.
    """
    import inspect

    from opencode_search import dashboard as dash_mod
    from opencode_search.dashboard import _spawn_daemon_restart_thread  # noqa: F401
    src = inspect.getsource(dash_mod)
    assert "_spawn_daemon_restart_thread" in src, (
        "dashboard.py /api/reload must call _spawn_daemon_restart_thread — unified reload path"
    )
    # MCP must NOT have any inline SIGTERM reload logic
    from opencode_search import mcp as mcp_mod
    mcp_src = inspect.getsource(mcp_mod)
    assert "SIGTERM" not in mcp_src, "mcp.py must not contain inline SIGTERM reload logic"


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


@pytest.mark.flaky(reruns=2)
def test_reload_sends_sse_reload_event_to_open_streams(http):
    """POST /api/reload must deliver a reload SSE event to open /api/events streams.

    Proves the graceful-reload path: _broadcast_reload_notice() sets reload_pending
    before SIGTERM so SSE generators emit {"type":"reload"} instead of being severed.
    """
    import contextlib
    import json as _json
    import threading

    events: list[dict] = []
    error_holder: list[Exception] = []

    def _stream() -> None:
        try:
            import httpx as _httpx
            with _httpx.stream("GET", f"{DAEMON_URL}/api/events/stream", timeout=30.0) as r:
                for raw_line in r.iter_lines():
                    if not raw_line.startswith("data:"):
                        continue
                    payload = raw_line[5:].strip()
                    if not payload:
                        continue
                    with contextlib.suppress(_json.JSONDecodeError):
                        evt = _json.loads(payload)
                        events.append(evt)
                        if evt.get("type") == "reload":
                            return
        except Exception as exc:
            error_holder.append(exc)

    t = threading.Thread(target=_stream, daemon=True)
    t.start()
    # Wait until we have received at least one metrics event (stream is live)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not any(e.get("type") == "metrics" for e in events):
        time.sleep(0.2)

    assert any(e.get("type") == "metrics" for e in events), (
        "SSE stream did not produce a metrics event within 10s — stream not live"
    )

    http.post("/api/reload", timeout=5.0)
    t.join(timeout=6.0)

    reload_events = [e for e in events if e.get("type") == "reload"]
    assert reload_events, (
        f"No reload event received in /api/events stream after POST /api/reload. "
        f"Events received: {events[:5]}"
    )
    assert reload_events[0].get("retry_after_ms") == 3000

    assert _wait_healthy(http), f"Daemon did not recover within {_RECOVER_TIMEOUT}s after reload"


@pytest.mark.flaky(reruns=2)
def test_reload_does_not_strand_simulated_external_client(http):
    """A client streaming /api/events must exit cleanly after reload, not hang.

    Simulates the 2h-hang scenario where a Claude Code session in another project
    was stuck on an opencode-search MCP call because the daemon was reloaded mid-flight.
    """
    import subprocess
    import sys

    # The subprocess streams /api/events/stream indefinitely.
    # After a reload, it should exit cleanly within 10s — either because it
    # received {"type":"reload"} from our graceful-reload path, or because
    # the connection was closed by the dying daemon.  Either way, not hanging.
    client_script = (
        "import httpx, json, sys, contextlib\n"
        "try:\n"
        "    with httpx.stream('GET', 'http://localhost:8765/api/events/stream', timeout=30.0) as r:\n"
        "        for line in r.iter_lines():\n"
        "            if not line.startswith('data:'):\n"
        "                continue\n"
        "            payload = line[5:].strip()\n"
        "            if not payload:\n"
        "                continue\n"
        "            with contextlib.suppress(Exception):\n"
        "                event = json.loads(payload)\n"
        "                if event.get('type') == 'reload':\n"
        "                    sys.exit(0)\n"
        "except Exception:\n"
        "    pass\n"
        "sys.exit(0)\n"
    )

    proc = subprocess.Popen(
        [sys.executable, "-c", client_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for the subprocess to connect and receive at least one metrics event (~2s)
    time.sleep(2.5)

    http.post("/api/reload", timeout=5.0)

    try:
        proc.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail(
            "External client subprocess did not exit within 10s after reload — "
            "simulates the 2h-hang bug where SIGTERM severed TCP without a close frame"
        )

    assert _wait_healthy(http), f"Daemon did not recover within {_RECOVER_TIMEOUT}s after reload"
