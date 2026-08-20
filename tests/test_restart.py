"""Kill the daemon mid-index and require the next one to finish the job.

The failure this covers is the one the fleet index is about to spend a night
exposed to: an unattended pass that is interrupted. Nothing under `-m live`
can express it -- `live.py` refuses to start or stop the daemon on purpose, so
that a test can never hide a broken unit -- hence a marker of its own rather
than a hole punched in that rule.

The daemon here is a subprocess with its own `CODERAG_STATE_DIR` and its own
port, not the installed unit. Two reasons, and the second is the load-bearing
one: a restart of the real daemon reconciles every enabled project, so a
fixture project would sit behind ~148 of them in the queue and this test would
run for a night; and a test that writes to the real registry is how that file
was destroyed the last two times.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from live import Rpc, require_clear_gpu, until

pytestmark = pytest.mark.restart

# Enough files that the pass is still running when the signal arrives, and
# enough that several BATCH_FILES commits land first -- a store with nothing
# committed would "survive" a restart trivially.
N_FILES = 240
COMMITTED_BEFORE_KILL = 64


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    for i in range(N_FILES):
        (path / f"mod{i:03d}.py").write_text(
            f'"""Module {i}."""\n\n\ndef handler_{i}(request):\n'
            f'    """Answer request {i} and record the outcome."""\n'
            f"    return {{'id': {i}, 'ok': True}}\n"
        )
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Daemon:
    """One `coderag serve` on a private state dir. `start` twice is the restart."""

    def __init__(self, state: Path, port: int):
        self.env = os.environ | {"CODERAG_STATE_DIR": str(state), "CODERAG_PORT": str(port)}
        self.url = f"http://127.0.0.1:{port}/mcp"
        self.state = state
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "coderag.cli", "serve"],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        rpc = Rpc(self.url)
        until(
            lambda: _reachable(rpc),
            timeout=120,
            what=f"the daemon to answer on {self.url}",
        )
        rpc.close()

    def stop(self, *, graceful: bool = True) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.send_signal(signal.SIGTERM if graceful else signal.SIGKILL)
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)


def _reachable(rpc: Rpc) -> bool:
    try:
        rpc.call("tools/list")
    except Exception:
        return False
    return True


def _store_files(state: Path) -> dict[str, str]:
    """path -> sha256, read straight off disk in read-only mode.

    Read-only and by URI rather than through `store.connect`, which creates the
    file: a test that silently makes an empty store cannot tell "the daemon has
    not written yet" from "the daemon wrote nowhere near here".
    """
    dbs = list((state / "indexes").glob("*/index.db"))
    if not dbs:
        return {}
    conn = sqlite3.connect(f"file:{dbs[0]}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return {r["path"]: r["sha256"] for r in conn.execute("SELECT path, sha256 FROM files")}
    except sqlite3.OperationalError:
        return {}  # mid-creation: the schema is not there yet
    finally:
        conn.close()


@pytest.fixture
def daemon(tmp_path):
    require_clear_gpu()
    served = Daemon(tmp_path / "state", _free_port())
    served.start()
    yield served
    served.stop()


def test_a_restart_mid_index_drains_the_queue_without_rebuilding(daemon, tmp_path):
    repo = _repo(tmp_path / "repo")

    rpc = Rpc(daemon.url)
    rpc.tool("index", root=str(repo))
    until(
        lambda: len(_store_files(daemon.state)) >= COMMITTED_BEFORE_KILL,
        timeout=300,
        what=f"{COMMITTED_BEFORE_KILL} files committed",
    )
    before = _store_files(daemon.state)
    assert len(before) < N_FILES, "the kill has to land mid-pass or this asserts nothing"

    daemon.stop()
    rpc.close()
    killed_at = len(before)

    daemon.start()
    until(
        lambda: len(_store_files(daemon.state)) == N_FILES,
        timeout=900,
        what="the index to converge after the restart",
    )

    assert before.items() <= _store_files(daemon.state).items(), "work was discarded and redone"

    # The observable that separates "resumed" from "rebuilt, then converged to
    # the same rows": `progress.begin` is handed the size of the work the pass
    # found to do, so a resumed pass announces the remainder and a rebuild
    # announces the lot.
    progress = json.loads((daemon.state / "progress.json").read_text())
    assert progress["project"] == str(repo)
    assert progress["files_total"] <= N_FILES - killed_at


def test_sigterm_exits_promptly_with_a_client_connected(daemon):
    """The connection is the whole test.

    MCP streamable HTTP holds its stream open, so uvicorn's graceful shutdown
    waits on a client that is not going to disconnect; systemd SIGKILLs at
    TimeoutStopSec instead, which fires OnFailure on every deliberate stop and
    skips `_shutdown_exit` entirely. `Daemon.stop` cannot see any of that -- it
    kills after 30 s and reports success -- so this drives the process directly.
    """
    rpc = Rpc(daemon.url)
    rpc.call("tools/list")  # a live stream, not just an open socket

    proc = daemon.proc
    started = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    try:
        returncode = proc.wait(timeout=25)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("SIGTERM did not stop the daemon in 25 s; systemd would SIGKILL at 90 s")
    finally:
        rpc.close()

    assert time.monotonic() - started < 20
    assert returncode == 0, "a non-zero code here is OnFailure firing on an ordinary stop"


def test_the_second_pass_after_a_clean_restart_writes_nothing(daemon, tmp_path):
    """Idempotence across a process boundary. In-process this is
    `test_index.py`'s signature check; the thing it cannot see is a store whose
    freshness depends on state that died with the previous daemon."""
    repo = _repo(tmp_path / "repo")

    rpc = Rpc(daemon.url)
    rpc.tool("index", root=str(repo))
    until(
        lambda: len(_store_files(daemon.state)) == N_FILES,
        timeout=900,
        what="the first index to converge",
    )
    rpc.close()

    daemon.stop()
    daemon.start()

    rpc = Rpc(daemon.url)
    started = time.monotonic()
    result = rpc.tool("index", root=str(repo))
    rpc.close()
    assert result["indexed"]["files"] == N_FILES
    assert time.monotonic() - started < 30, "a no-op pass that re-embeds is not a no-op"
