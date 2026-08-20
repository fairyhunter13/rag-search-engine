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
from live import Rpc, require_clear_gpu, require_daemon, until

pytestmark = pytest.mark.live

# Never Opus in a test: the ceiling is a cost rule, and this asks for a lookup.
MODEL = "sonnet"
SERVER = "coderag"
TOOL_PREFIX = f"mcp__{SERVER}__"
NEEDLE = "def settle_partial_shipment"

# The session is put in a root whose only interesting content lives in a
# federated member, reached through a directory symlink. That is the one
# question the client's own tools cannot answer from the working tree -- `grep
# -r` does not follow a symlinked directory -- and it is the capability this
# engine exists for, so it is the fair contest.
#
# The question this layer used to ask quoted an assertion message verbatim
# against this repo, and every session answered it with `grep`, correctly, and
# went on doing so through three escalations of the served instructions and the
# tool description. Two confounds, both real and both recorded rather than
# tuned away: a literal string is grep's own ground, and in this repo the model
# guesses the filename from the layout without searching at all.
QUESTION = (
    "Which project reachable from here settles a partial shipment, and where? "
    "Answer with the file and line range only."
)


def _fixture_repo(base: Path) -> Path:
    """A root plus a member it reaches only through a symlink."""
    member, root = base / "billing-core", base / "storefront"
    (member / "src").mkdir(parents=True)
    (member / "src" / "ledger.py").write_text(
        f"{NEEDLE}(order, shipped):\n    return order.total * shipped / order.units\n",
        encoding="utf-8",
    )
    root.mkdir()
    (root / "app.py").write_text("import ledger\n", encoding="utf-8")
    for path in (member, root):
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (root / "vendor-billing").symlink_to(member, target_is_directory=True)
    return root


def _carry_credentials(home: Path) -> None:
    host = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    creds = host / ".credentials.json"
    if not creds.exists():
        pytest.fail(f"no {creds}; the isolated session cannot authenticate")
    copy = home / ".credentials.json"
    copy.write_bytes(creds.read_bytes())
    copy.chmod(0o600)


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    """The tree, indexed and searchable, before any session is started.

    Asking before the member is searchable measures the index queue: the
    session would call `search`, get nothing, fall back to its own tools, and
    the transcript would look exactly like a tool-description failure.
    """
    require_clear_gpu()
    rpc = Rpc(require_daemon())
    root = _fixture_repo(tmp_path_factory.mktemp("layer3"))
    try:
        rpc.tool("index", root=str(root))
        until(
            lambda: rpc.tool("search", query=NEEDLE, root=str(root), mode="lexical")["results"],
            timeout=300,
            what="the member's content to become searchable from the root",
        )
        yield root
    finally:
        rpc.tool("index", root=str(root), enabled=False)
        rpc.close()


@pytest.fixture(scope="module")
def transcript(workspace):
    """One session, asked a question only this server can answer cheaply."""
    if not shutil.which("claude"):
        pytest.fail("the `claude` CLI is not on PATH; layer 3 cannot run")

    with tempfile.TemporaryDirectory(prefix="coderag-layer3-") as tmp:
        home = Path(tmp)
        # Built by hand rather than copied from the host: a copy brings the
        # operator's hooks, and one firing mid-run turns red into a mystery.
        (home / "settings.json").write_text("{}\n", encoding="utf-8")
        # `--mcp-config`, not an `mcpServers` key in settings.json: the CLI does
        # not read one from there, and it says nothing when it does not. The
        # session started clean, listed no MCP tools, answered the question by
        # opening the file itself -- and this layer read that as a
        # tool-description failure, which is precisely the finding it exists to
        # produce. Hence the connection assertion below.
        mcp_config = home / "mcp.json"
        mcp_config.write_text(
            json.dumps({"mcpServers": {SERVER: {"type": "http", "url": config.MCP_URL}}}, indent=2),
            encoding="utf-8",
        )
        # The one thing that does have to come from the host. Isolation is about
        # not inheriting hooks and memory, not about logging in again -- without
        # it the CLI exits 1 with "Please run /login" and the layer reads as a
        # server failure. Copied 0600 into a dir that is deleted with the test,
        # and never printed: the assertions below only ever quote `stderr`.
        _carry_credentials(home)
        out = subprocess.run(
            [
                "claude",
                "-p",
                QUESTION,
                "--model",
                MODEL,
                "--output-format",
                "json",
                "--verbose",
                "--mcp-config",
                str(mcp_config),
                # Not decoration on an already-isolated home: without it the CLI
                # merges config sources and connects on 2025-11-25, which cannot
                # carry a workspace pin at all, so every call is refused now that
                # the flag ships at 1. Measured -- it is the only one of the four
                # differences from a passing invocation that moves the era.
                "--strict-mcp-config",
                "--allowedTools",
                f"{TOOL_PREFIX}search",
            ],
            cwd=str(workspace),
            env=os.environ | {"CLAUDE_CONFIG_DIR": str(home)},
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    assert out.returncode == 0, f"claude -p exited {out.returncode}: {out.stderr[-2000:]}"
    messages = _messages(json.loads(out.stdout))
    _assert_connected(messages)
    return messages


def _assert_connected(messages) -> None:
    """The precondition, separated from the finding.

    Without this, a session that never saw the server is indistinguishable from
    one that saw it and ignored it -- and the second is the failure this layer
    is for. The first is a harness bug and has to say so.
    """
    init = next((m for m in messages if m.get("subtype") == "init"), None)
    assert init is not None, "the session emitted no init message"
    listed = {s.get("name"): s.get("status") for s in init.get("mcp_servers") or []}
    assert listed.get(SERVER) == "connected", f"the server never connected: {listed}"


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

    It used to also require the session to write a `root`, on the grounds that
    an unscoped call federates over the fleet. With the flag at `1` that is no
    longer reachable: a call with no pin is refused, and a call with one means
    the caller's own workspace. Which root a pinned call may name is held by
    `test_live_scoping`, off the model.
    """
    called = [
        b for b in _blocks(transcript, "tool_use") if str(b.get("name", "")).startswith(TOOL_PREFIX)
    ]
    assert called, (
        "no coderag tool was called; the session answered from its own file access. "
        "That is a tool-description failure, and no protocol test can see it."
    )


def _results(block) -> list[dict]:
    """The result payload, from either shape the client hands back.

    A `tool_result` arrives as a JSON string here, not as the list of content
    blocks the protocol describes -- and the list form is what this read first,
    so it found every result block and parsed none of them.
    """
    content = block.get("content")
    parts = (
        [content]
        if isinstance(content, str)
        else [p.get("text", "") for p in content if isinstance(p, dict)]
    )
    out = []
    for part in parts:
        try:
            out += json.loads(part).get("results", [])
        except (ValueError, AttributeError, TypeError):
            continue
    return out


def test_the_locations_the_session_was_handed_resolve(transcript, workspace):
    """A ranked list of stale line numbers looks identical to a working one."""
    blocks = _blocks(transcript, "tool_result")
    hits = [hit for block in blocks for hit in _results(block)]
    # The payload, not just its absence: an error envelope, an empty result set
    # and a shape this cannot parse all reduce to `[]` here, and they are three
    # different bugs in three different places.
    assert hits, f"no parseable results in {[str(b.get('content'))[:300] for b in blocks]}"

    for hit in hits[:5]:
        # `path`, not `rel_path`: a federated hit is relative to the member that
        # owns it, and the session is standing in the root.
        path = Path(hit["path"])
        absolute = path if path.is_absolute() else workspace / path
        assert absolute.exists(), f"search returned {absolute}, which does not exist"
        lines = absolute.read_text(encoding="utf-8", errors="replace").splitlines()
        start, end = hit["lines"]
        assert 1 <= start <= end <= len(lines), (
            f"{path} has {len(lines)} lines, the hit claims {start}-{end}"
        )
