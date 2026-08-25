"""What the health check pages for, and -- the point of it -- what it does not.

Every test here would pass against a checker that simply pages whenever
`projects_failing > 0`, except the two that hold the persistence rule. Those are
the ones with a number behind them: `last_error` is cleared by the next success
and the sweep is hourly, so a one-off failure is visible for up to an hour and a
sampling checker pages for nearly every one.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from coderag import health


@pytest.fixture
def daemon():
    """A stand-in `/healthz` whose reply the test sets between checks.

    A real daemon cannot be driven to fail one project and then another inside a
    test, and pointing this at the live one would read the whole 244-row fleet.
    """
    body = {"projects": 3, "projects_failing": 0, "failing": [], "scheduler_errors": {}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/healthz", body
    server.shutdown()


def test_a_healthy_fleet_does_not_page(daemon, tmp_path):
    url, _ = daemon
    ok, message = health.check(url, tmp_path / "health.json")
    assert ok
    assert "none failing" in message


def test_one_failing_sample_does_not_page(daemon, tmp_path):
    """The discriminating one. A checker keyed on `projects_failing > 0` pages
    here, and this is the common case: a transient failure the next sweep clears."""
    url, body = daemon
    body.update(projects_failing=1, failing=["/repo/a"])

    ok, message = health.check(url, tmp_path / "health.json")
    assert ok, message
    assert "/repo/a" in message, "the first sighting is still worth saying"


def test_the_same_project_failing_twice_pages(daemon, tmp_path):
    url, body = daemon
    state = tmp_path / "health.json"
    body.update(projects_failing=1, failing=["/repo/a"])

    assert health.check(url, state)[0] is True
    ok, message = health.check(url, state)
    assert ok is False
    assert "/repo/a" in message


def test_two_different_projects_failing_once_each_do_not_page(daemon, tmp_path):
    """Two consecutive non-zero counts, no project failing twice. A checker
    comparing counts rather than identities pages here and is wrong."""
    url, body = daemon
    state = tmp_path / "health.json"

    body.update(projects_failing=1, failing=["/repo/a"])
    assert health.check(url, state)[0] is True
    body.update(projects_failing=1, failing=["/repo/b"])
    assert health.check(url, state)[0] is True


def test_a_project_that_recovers_clears_the_history(daemon, tmp_path):
    """Otherwise the second failure months later pages as if it never stopped."""
    url, body = daemon
    state = tmp_path / "health.json"

    body.update(projects_failing=1, failing=["/repo/a"])
    health.check(url, state)
    body.update(projects_failing=0, failing=[])
    health.check(url, state)
    body.update(projects_failing=1, failing=["/repo/a"])

    assert health.check(url, state)[0] is True


def test_a_daemon_that_does_not_answer_pages(tmp_path):
    """`systemctl is-active` was green through the outage where every `/mcp`
    request returned 500 -- the process was up and answering nothing."""
    ok, message = health.check("http://127.0.0.1:1/healthz", tmp_path / "health.json")
    assert ok is False
    assert "did not answer" in message


def test_an_unreachable_daemon_does_not_overwrite_the_failing_history(daemon, tmp_path):
    """A restart between checks would otherwise reset the comparison, and a
    project failing across it starts over as a first sighting every time."""
    url, body = daemon
    state = tmp_path / "health.json"
    body.update(projects_failing=1, failing=["/repo/a"])
    health.check(url, state)

    health.check("http://127.0.0.1:1/healthz", state)

    assert health.check(url, state)[0] is False


def test_a_dead_scheduler_pages_on_the_same_two_sample_rule(daemon, tmp_path):
    """No project carries this failure -- the sweep is what would have recorded
    one -- so a checker reading only `failing` calls a dead fleet healthy."""
    url, body = daemon
    state = tmp_path / "health.json"
    body["scheduler_errors"] = {"sweep": "RuntimeError: boom"}

    ok, first = health.check(url, state)
    assert ok, first
    ok, message = health.check(url, state)
    assert ok is False
    assert "scheduler:sweep" in message


def test_a_dead_watcher_pages_on_the_same_two_sample_rule(daemon, tmp_path):
    """`/healthz` publishes twelve fields and this read two of them. So a
    watcher thread that died hours ago passed: the fleet stopped noticing every
    write while every reading anyone took stayed green."""
    url, body = daemon
    state = tmp_path / "health.json"
    body["watching"] = False

    ok, first = health.check(url, state)
    assert ok, first
    ok, message = health.check(url, state)
    assert ok is False
    assert "watcher:" in message


def test_a_queue_that_keeps_draining_does_not_page(daemon, tmp_path):
    """The discriminating half. The hourly reconcile enqueues every enabled row,
    so a deep queue is the ordinary state of a healthy fleet, and a checker keyed
    on depth alone pages once an hour forever."""
    url, body = daemon
    state = tmp_path / "health.json"
    body["indexer"] = {"queue_depth": 400}

    assert health.check(url, state)[0]
    body["indexer"] = {"queue_depth": 380}
    ok, message = health.check(url, state)
    assert ok, message


def test_a_queue_that_stops_draining_pages(daemon, tmp_path):
    url, body = daemon
    state = tmp_path / "health.json"
    body["indexer"] = {"queue_depth": 400}

    assert health.check(url, state)[0]
    ok, message = health.check(url, state)
    assert ok is False
    assert "indexer:" in message


def test_failures_past_the_reply_cap_are_not_dropped(daemon, tmp_path):
    """`failing` is truncated at `HEALTH_FAILING_CAP` and `projects_failing`
    carries the true count. Reading the list alone, a fleet failing past the cap
    is a fleet whose extra failures were in no set the checker compares."""
    url, body = daemon
    state = tmp_path / "health.json"
    body.update(projects_failing=44, failing=[f"/repo/{i}" for i in range(20)])

    health.check(url, state)
    ok, message = health.check(url, state)
    assert ok is False
    assert "past the reply's cap" in message
