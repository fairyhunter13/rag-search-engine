"""Layer 2: the real protocol over the real transport, against the real daemon.

Everything in `test_server_tools.py` calls the tool functions in-process. That
catches a broken tool and cannot catch a broken *server*: a schema the library
serialises differently over the wire, a result that is a Python dict in-process
and malformed JSON on the socket, a tool that never got registered on the app
the daemon actually serves. This file exists for exactly that gap, which is why
it speaks JSON-RPC by hand.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coderag import config
from live import MODERN_PROTOCOL, Rpc, require_clear_gpu, require_daemon

pytestmark = pytest.mark.live

PROTOCOL = "2025-06-18"
# The newest revision the handshake can reach. `2026-07-28` is not one of them:
# it dropped `initialize` entirely, so asking for it here is answered with this.
LATEST_HANDSHAKE = "2025-11-25"
# `--help` verified against 0.1.16 on 2026-08-19: `server --url` and `--spec-version`.
CONFORMANCE = "~0.1.16"


@pytest.fixture(scope="module")
def rpc():
    require_clear_gpu()
    url = require_daemon()
    client = Rpc(url)
    yield client
    client.close()


def test_the_handshake_answers_with_a_server_that_names_itself(rpc):
    result = rpc.call(
        "initialize",
        {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "coderag-live-suite", "version": "0.1.0"},
        },
    )
    assert result["serverInfo"]["name"] == config.APP
    assert result["capabilities"].get("tools") is not None


def test_the_served_instructions_are_the_ones_the_host_installs(rpc):
    """`policy/hermes.py` in claude-code-workflows fetches this string and
    writes it as a system prompt. An empty one blanks the host's doctrine with
    no error anywhere, so the assertion is on content, not on presence."""
    result = rpc.call(
        "initialize",
        {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "x", "version": "0"},
        },
    )
    instructions = result.get("instructions", "")
    assert "index" in instructions and "search" in instructions
    assert len(instructions.strip()) > 100, instructions


def test_exactly_two_tools_reach_the_wire(rpc):
    listed = rpc.call("tools/list")["tools"]
    assert sorted(t["name"] for t in listed) == ["index", "search"]


def test_both_schemas_survive_serialisation(rpc):
    """A parameter that exists in Python and not in the JSON schema is a
    parameter no agent will ever pass."""
    schemas = {t["name"]: t["inputSchema"]["properties"] for t in rpc.call("tools/list")["tools"]}
    assert {"root", "enabled"} <= set(schemas["index"])
    assert {"query", "root", "k", "mode", "rerank", "path_glob", "lang"} <= set(schemas["search"])
    assert schemas["search"]["k"]["type"] == "integer"


def test_an_unregistered_root_is_told_what_to_call_rather_than_widened(rpc, tmp_path):
    """The refusal is the feature. A search that silently widened to the fleet
    would answer a question about this directory with someone else's code."""
    out = rpc.tool("search", query="parse the user configuration", root=str(tmp_path))
    assert out["results"] == []
    assert "index" in out["error"], out


def test_an_unknown_mode_names_the_valid_set(rpc, tmp_path):
    # A root, any root: rootless is refused before the mode is ever read.
    out = rpc.tool("search", query="anything", root=str(tmp_path), mode="telepathy")
    for mode in config.MODES:
        assert mode in out["error"], out


@pytest.mark.skipif(not shutil.which("npx"), reason="mcp-inspector needs npx")
def test_the_official_inspector_agrees(rpc):
    """A second implementation of the client, on purpose. Our own JSON-RPC
    caller and the server were written in the same session; the inspector was
    not, and it is what the ecosystem's clients are built against."""
    out = subprocess.run(
        [
            "npx",
            "-y",
            "@modelcontextprotocol/inspector",
            "--cli",
            config.MCP_URL,
            "--transport",
            "http",
            "--method",
            "tools/list",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    theirs = json.loads(out.stdout)["tools"]
    ours = rpc.call("tools/list")["tools"]
    assert (
        sorted(t["name"] for t in theirs) == sorted(t["name"] for t in ours) == ["index", "search"]
    )


def test_the_handshake_settles_on_the_newest_revision_it_can_reach(rpc):
    """Asking for `2026-07-28` is the interesting case: the handshake cannot
    reach it, so the correct answer is a counter-offer of the newest revision
    that it can -- and a server stuck on an older one is the failure nothing
    else in this suite would notice."""
    result = rpc.call(
        "initialize",
        {
            "protocolVersion": MODERN_PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "coderag-live-suite", "version": "0.1.0"},
        },
    )
    assert result["protocolVersion"] == LATEST_HANDSHAKE, (
        f"server negotiated {result['protocolVersion']!r}, not the newest handshake revision"
    )
    assert not (set(result["capabilities"]) & {"sampling", "logging", "roots"}), (
        f"server advertises a deprecated capability: {sorted(result['capabilities'])}"
    )


def test_the_current_revision_is_served_with_no_handshake_at_all(rpc):
    """`2026-07-28` replaced `initialize` with one `server/discover` call and
    no session. ccw's `policy/hermes.py` reads the host system prompt from it,
    so an unreachable era would silently pin the host to a compatibility path
    that has a removal date on it."""
    result = rpc.modern("server/discover")

    assert MODERN_PROTOCOL in result["supportedVersions"], result.get("supportedVersions")
    assert "index" in result["instructions"] and "search" in result["instructions"]
    assert not (set(result["capabilities"]) & {"sampling", "logging", "roots"})
    listed = rpc.modern("tools/list")["tools"]
    assert sorted(x["name"] for x in listed) == ["index", "search"]
    # Consumed, never advertised. The server asks the *client* for its roots and
    # the resolver keeps that out of the schema, so the assertion above and this
    # one are the two halves of the same claim -- over the wire, off the daemon.
    for tool in listed:
        props = tool["inputSchema"]["properties"]
        assert "root" in props and "pinned" not in props, props


def test_the_client_roots_pin_arrives_and_bounds_the_search(rpc, tmp_path):
    """The rollout's premise, over the wire, against the daemon that ships.

    Two synthetic paths, neither of which needs to exist: `resolve` is all the
    server does with them, and a real project must never appear in an assertion
    here.
    """
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    args = {"query": "handler", "root": str(theirs), "mode": "lexical"}

    # `index` is what the pin still bounds. `search` reads any indexed row by
    # name since the sixth amendment, so the read path no longer carries this.
    pinned = rpc.modern_tool("index", roots=[str(mine)], root=str(theirs))
    assert "outside this session's workspace" in pinned["structuredContent"]["error"]

    # The discriminator. Without it a server that refused every call would pass
    # the assertion above, and "the pin bounded the call" would be unearned:
    # declare no `roots` and the same call has to fail for a *different* reason.
    unpinned = rpc.modern_tool("search", **args)
    assert "sent no workspace roots" in unpinned["structuredContent"]["error"]


@pytest.mark.skipif(not shutil.which("npx"), reason="the conformance suite needs npx")
def test_the_official_conformance_suite_passes(rpc):
    """Separate from the inspector, and it checks what the inspector does not:
    error shapes, required-field handling and the revision's own rules. The
    inspector reports what a server said; this reports whether it was allowed
    to say it.

    It takes `rpc` for the fixture's preconditions, not for the client: run
    with `-k conformance` it otherwise failed on connection-refused, which
    names npx as the suspect instead of the daemon that is not running.
    """
    out = subprocess.run(
        [
            "npx",
            "-y",
            # Pinned: pre-1.0, so a minor bump is a breaking one, and an
            # unpinned suite turns a green test red without a commit here.
            f"@modelcontextprotocol/conformance@{CONFORMANCE}",
            "server",
            "--url",
            config.MCP_URL,
            "--spec-version",
            LATEST_HANDSHAKE,
            # The suite runs every scenario in its set, including the ones for
            # capabilities this server does not have. The baseline names those,
            # and the run still fails on anything not in it -- or on anything in
            # it that starts passing.
            "--expected-failures",
            str(Path(__file__).parent / "conformance-baseline.yaml"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert out.returncode == 0, (
        f"exit {out.returncode}:\n{out.stdout[-3000:]}\n{out.stderr[-1500:]}"
    )
