"""CLI↔dashboard parity: ask/graph/overview/wiki are reachable from the CLI.

Uses typer.testing.CliRunner (in-process, real GPU embedder singleton, no mock).
All tests bind to the sample_workspace projects via --project / explicit path,
never to real registry projects (no-real-project guard).
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.live


def _runner():
    from typer.testing import CliRunner
    return CliRunner()


# ── overview ─────────────────────────────────────────────────────────────────

def test_cli_overview_matches_handle_overview(sample_workspace):
    """CLI overview --what status output equals handle_overview() return value."""
    from opencode_search.cli import app
    from opencode_search.server._overview import handle_overview

    fed = sample_workspace.fed_root
    r = _runner().invoke(app, ["overview", "--project", fed, "--what", "status"])
    assert r.exit_code == 0, f"overview exit {r.exit_code}: {r.output}"
    assert r.output.strip(), "overview returned empty output"
    # CLI output must equal the shared helper's return (parity check)
    expected = handle_overview(fed, "status")
    assert r.output.strip() == expected.strip(), (
        "CLI overview output differs from handle_overview() — parity broken"
    )


# ── graph ─────────────────────────────────────────────────────────────────────

def test_cli_graph_matches_run_graph(sample_workspace):
    """CLI graph output equals run_graph() return value (definition relation)."""
    from opencode_search.cli import app
    from opencode_search.query.graph_handler import run_graph

    member = sample_workspace.promo
    # Use a generic symbol name likely to appear; empty result is valid (index state varies)
    symbol = "main"
    r = _runner().invoke(app, ["graph", symbol, "--project", member, "--relation", "definition"])
    assert r.exit_code == 0, f"graph exit {r.exit_code}: {r.output}"
    assert r.output.strip(), "graph returned empty output"
    expected = run_graph(symbol, member, "definition", "")
    assert r.output.strip() == expected.strip(), (
        "CLI graph output differs from run_graph() — parity broken"
    )
    # Must be valid JSON
    json.loads(r.output.strip())


# ── ask ───────────────────────────────────────────────────────────────────────

def test_cli_ask_matches_run_ask(sample_workspace):
    """CLI ask output equals run_ask() return value."""
    from opencode_search.cli import app
    from opencode_search.query.ask import run_ask

    fed = sample_workspace.fed_root
    query = "how does federation indexing work?"
    r = _runner().invoke(app, ["ask", query, "--project", fed, "--scope", "all"])
    assert r.exit_code == 0, f"ask exit {r.exit_code}: {r.output}"
    assert len(r.output.strip()) > 10, "ask returned too-short output"
    expected = run_ask(query, fed, "all")
    assert r.output.strip() == expected.strip(), (
        "CLI ask output differs from run_ask() — parity broken"
    )


# ── wiki ──────────────────────────────────────────────────────────────────────

def test_cli_wiki_builds_pages(sample_workspace):
    """CLI wiki runs without error and reports pages_written."""
    from opencode_search.cli import app

    fed = sample_workspace.fed_root
    r = _runner().invoke(app, ["wiki", fed])
    assert r.exit_code == 0, f"wiki exit {r.exit_code}: {r.output}"
    assert "pages_written=" in r.output, f"wiki output missing pages_written: {r.output!r}"
    n = int(r.output.strip().split("pages_written=")[1])
    assert n >= 0, f"pages_written must be non-negative; got {n}"


# ── commands present in --help ────────────────────────────────────────────────

def test_new_commands_in_help():
    """ask/graph/overview/wiki appear in top-level --help."""
    from opencode_search.cli import app

    r = _runner().invoke(app, ["--help"])
    assert r.exit_code == 0
    for cmd in ("ask", "graph", "overview", "wiki"):
        assert cmd in r.output, f"CLI missing command '{cmd}' in --help"
