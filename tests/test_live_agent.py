"""Layer 3: a real Claude Code session, over the real transport, on a real repo.

Layer 2 proves the wire format. It cannot prove the thing this engine exists
for, because a server can be perfectly well-formed and still useless to the only
client that matters: a tool description nobody acts on, a result shape a model
cannot read, a `root` argument it never learns to pass. Those failures are
invisible to every assertion below this file.

So this layer asserts on the *transcript* rather than on prose. What the model
says is not evidence -- it will describe a file it never opened. What it did is:
a `tool_use` block naming this server, and locations in the `tool_result` that
resolve on disk.

The session runs against an isolated config dir. A bare `claude -p` inherits the
operator's own profile, and then the test measures that profile's memory, hooks
and skills rather than this server.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from coderag import config
from live import require_clear_gpu, require_daemon

pytestmark = pytest.mark.live

# Never Opus in a test: the ceiling is a cost rule, and this asks for a lookup.
MODEL = "sonnet"
SERVER = "coderag"
TOOL_PREFIX = f"mcp__{SERVER}__"
REPO = Path(__file__).resolve().parents[1]
NEEDLE = "CPU inference is forbidden"


@pytest.fixture(scope="module")
def transcript():
    """One session, asked a question only this server can answer cheaply."""
    require_clear_gpu()
    require_daemon()
    if not shutil.which("claude"):
        pytest.fail("the `claude` CLI is not on PATH; layer 3 cannot run")

    with tempfile.TemporaryDirectory(prefix="coderag-layer3-") as tmp:
        home = Path(tmp)
        # Built by hand rather than copied from the host: a copy brings the
        # operator's hooks, and one firing mid-run turns red into a mystery.
        (home / "settings.json").write_text(
            json.dumps({"mcpServers": {SERVER: {"type": "http", "url": config.MCP_URL}}}, indent=2),
            encoding="utf-8",
        )
        out = subprocess.run(
            [
                "claude",
                "-p",
                f"Where is the assertion whose message is {NEEDLE!r}? "
                "Answer with the file and line range only.",
                "--model",
                MODEL,
                "--output-format",
                "json",
                "--verbose",
                "--allowedTools",
                f"{TOOL_PREFIX}search",
            ],
            cwd=str(REPO),
            env=os.environ | {"CLAUDE_CONFIG_DIR": str(home)},
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    assert out.returncode == 0, f"claude -p exited {out.returncode}: {out.stderr[-2000:]}"
    return _messages(json.loads(out.stdout))


def _messages(payload) -> list[dict]:
    if isinstance(payload, list):
        return payload
    return payload.get("messages") or [payload]


def _blocks(messages: list[dict], kind: str) -> list[dict]:
    found = []
    for message in messages:
        content = (message.get("message") or message).get("content")
        for block in content if isinstance(content, list) else []:
            if block.get("type") == kind:
                found.append(block)
    return found


def test_the_session_reaches_for_the_server_rather_than_reading_the_file(transcript):
    """The tool description has to beat the model's default instinct.

    Asserting that an answer arrived would pass with the server switched off --
    `claude` would open the file itself and be right.
    """
    called = [
        b for b in _blocks(transcript, "tool_use") if str(b.get("name", "")).startswith(TOOL_PREFIX)
    ]
    assert called, (
        "no coderag tool was called; the session answered from its own file access. "
        "That is a tool-description failure, and no protocol test can see it."
    )
    assert any("root" in (b.get("input") or {}) for b in called), (
        "the session called search without a root; unscoped it federates over the fleet"
    )


def test_the_locations_the_session_was_handed_resolve(transcript):
    """A ranked list of stale line numbers looks identical to a working one."""
    hits = []
    for block in _blocks(transcript, "tool_result"):
        content = block.get("content")
        for part in content if isinstance(content, list) else []:
            try:
                hits += json.loads(part.get("text", "")).get("results", [])
            except (ValueError, AttributeError):
                continue
    assert hits, "the session's tool calls returned no parseable results"

    for hit in hits[:5]:
        path = Path(hit["rel_path"])
        absolute = path if path.is_absolute() else REPO / path
        assert absolute.exists(), f"search returned {absolute}, which does not exist"
        lines = absolute.read_text(encoding="utf-8", errors="replace").splitlines()
        start, end = hit["lines"]
        assert 1 <= start <= end <= len(lines), (
            f"{path} has {len(lines)} lines, the hit claims {start}-{end}"
        )
