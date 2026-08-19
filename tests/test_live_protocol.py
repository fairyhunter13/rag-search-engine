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

import pytest

from coderag import config
from live import Rpc, require_clear_gpu, require_daemon

pytestmark = pytest.mark.live

PROTOCOL = "2025-06-18"
# The revision `mcp` 2.x negotiates. Kept beside the older one on purpose: a
# server that only answers the version its own SDK prefers is a server that
# breaks every client that has not upgraded yet.
CURRENT_PROTOCOL = "2026-07-28"
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


def test_an_unknown_mode_names_the_valid_set(rpc):
    out = rpc.tool("search", query="anything", mode="telepathy")
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


def test_the_current_spec_revision_is_negotiated(rpc):
    """`2026-07-28` made the protocol core stateless and deprecated Roots,
    Sampling and Logging. This repo pins `mcp==2.0.*`, which shipped alongside
    it -- so the pin is only correct if the wire agrees, and nothing else here
    would notice a server stuck on an older revision."""
    result = rpc.call(
        "initialize",
        {
            "protocolVersion": CURRENT_PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "coderag-live-suite", "version": "0.1.0"},
        },
    )
    assert result["protocolVersion"] == CURRENT_PROTOCOL, (
        f"asked for {CURRENT_PROTOCOL}, server negotiated {result['protocolVersion']!r}"
    )
    assert not (set(result["capabilities"]) & {"sampling", "logging", "roots"}), (
        f"server advertises a deprecated capability: {sorted(result['capabilities'])}"
    )


@pytest.mark.skipif(not shutil.which("npx"), reason="the conformance suite needs npx")
def test_the_official_conformance_suite_passes():
    """Separate from the inspector, and it checks what the inspector does not:
    error shapes, required-field handling and the revision's own rules. The
    inspector reports what a server said; this reports whether it was allowed
    to say it."""
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
            CURRENT_PROTOCOL,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert out.returncode == 0, (
        f"exit {out.returncode}:\n{out.stdout[-3000:]}\n{out.stderr[-1500:]}"
    )
