"""The bridge is a pipe, and the property under test is what it does NOT do.

Every assertion here is about stdout discipline. A bridge that writes one
diagnostic line to stdout desynchronises the client's JSON-RPC framing for the
rest of the session, and the symptom -- a client that hangs on the *next* call --
points nowhere near the bridge.

No mocks: a real HTTP server on a real socket, a real pipe on stdin.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from coderag import bridge


class _Echo(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["content-length"]))
        payload = json.dumps({"echo": json.loads(body), "accept": self.headers["accept"]})
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload.encode())

    def log_message(self, *_args) -> None:  # keep the test output readable
        pass


@pytest.fixture
def echo_url():
    server = HTTPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp"
    finally:
        server.shutdown()


@pytest.fixture
def stdin_pipe(monkeypatch):
    """A real fd on stdin, because `connect_read_pipe` will not take anything else.

    stdout stays pytest's: capture reinstalls `sys.stdout` after fixtures run, so
    a StringIO swapped in here is silently replaced before the test body starts.
    """
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr("sys.stdin", os.fdopen(read_fd, "rb", buffering=0))
    return write_fd


def _send(write_fd: int, *lines: str) -> None:
    for line in lines:
        os.write(write_fd, line.encode() + b"\n")
    os.close(write_fd)


def test_each_line_goes_out_and_the_reply_comes_back_on_one_line(echo_url, stdin_pipe, capsys):
    _send(stdin_pipe, json.dumps({"id": 1, "method": "a"}), json.dumps({"id": 2, "method": "b"}))

    assert bridge.run(echo_url) == 0

    replies = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["echo"]["id"] for r in replies] == [1, 2], "order is the whole contract"


def test_it_asks_for_both_content_types_streamable_http_can_answer_with(
    echo_url, stdin_pipe, capsys
):
    """A bridge that accepts only JSON gets a 406 from a server that chose SSE,
    and the failure looks like the daemon being down."""
    _send(stdin_pipe, json.dumps({"id": 1}))

    bridge.run(echo_url)

    accept = json.loads(capsys.readouterr().out)["accept"]
    assert "application/json" in accept and "text/event-stream" in accept


def test_a_dead_daemon_writes_nothing_at_all_to_stdout(stdin_pipe, capsys):
    _send(stdin_pipe, json.dumps({"id": 1}))

    assert bridge.run("http://127.0.0.1:1/mcp") == 0

    captured = capsys.readouterr()
    assert captured.out == "", "a diagnostic on stdout breaks framing for every later call"
    assert "bridge:" in captured.err


def test_one_failed_call_does_not_end_the_session(echo_url, stdin_pipe, capsys, monkeypatch):
    """The daemon restarts; the editor's bridge process does not. Dropping the
    session on a single connection error turns a 5 s restart into a dead tool."""
    read_fd, second_fd = os.pipe()
    os.write(stdin_pipe, json.dumps({"id": 1}).encode() + b"\n")

    # One line the daemon cannot answer, then the daemon comes back on the next.
    monkeypatch.setattr(bridge.config, "MCP_URL", "http://127.0.0.1:1/mcp")
    threading.Timer(0.1, lambda: os.close(stdin_pipe)).start()
    bridge.run()
    assert capsys.readouterr().out == ""

    monkeypatch.setattr("sys.stdin", os.fdopen(read_fd, "rb", buffering=0))
    _send(second_fd, json.dumps({"id": 2}))
    bridge.run(echo_url)
    assert json.loads(capsys.readouterr().out)["echo"]["id"] == 2


def test_an_idle_bridge_returns_rather_than_holding_the_model(echo_url, stdin_pipe):
    """The idle exit is why five editors do not pin five daemons."""
    assert bridge.run(echo_url, idle_seconds=0.2) == 0
    os.close(stdin_pipe)
