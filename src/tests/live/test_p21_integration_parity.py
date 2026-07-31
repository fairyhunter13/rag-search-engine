"""P21: Integration-parity tests — config drift guard.

Puts the existing integration system (claude profiles + hermes; OpenCode is no
longer configured) under the live test suite.
Reuses configure_integrations.py --check --json rather than reimplementing the logic.
Skips gracefully if a target's config file does not exist (tool not installed).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_REPO = Path(__file__).resolve().parent.parent.parent.parent
_SCRIPT = _REPO / "scripts" / "configure_integrations.py"
_SCRIPTS_SRC = str(_REPO / "scripts")


def _run_check_json(env: dict[str, str] | None = None) -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--check", "--json"],
        capture_output=True, text=True, timeout=30,
        cwd=str(_REPO), env=env,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"configure_integrations --check --json produced non-JSON output "
            f"(exit {result.returncode}):\n{result.stdout[:500]}\n{result.stderr[:200]}"
        ) from exc


def test_no_installed_tree_is_missing_or_error():
    """All config targets whose files exist must be already_ok, not missing/error."""
    results = _run_check_json()
    bad = [
        r for r in results
        if r.get("status") in {"error", "missing"}
        and Path(r.get("path", "")).exists()
    ]
    assert not bad, (
        "Installed integration targets are out of sync with canonical.py:\n"
        + "\n".join(f"  {r['tool']}: {r['status']} — {r['message']}" for r in bad)
    )


def test_canonical_body_is_served_over_mcp_not_copied_into_claude_md():
    """The doctrine reaches an agent exactly once: as MCP server instructions, not as a
    CLAUDE.md copy.

    global_prompt._PROMPT and canonical.CANONICAL_BODY must stay identical — configure_
    integrations.py removes the CLAUDE.md block on the strength of the daemon serving the
    same text, so if the two drift, deleting the copy would silently drop real rules.

    Inverted 2026-07-31 (was: assert CANONICAL_BODY present in ~/.claude/CLAUDE.md). The
    block was loaded verbatim into every session of every profile *and* served over MCP —
    ~500 tokens (measured via /context) billed twice per turn for one set of rules.
    """
    sys.path.insert(0, _SCRIPTS_SRC)
    try:
        from integrations.canonical import CANONICAL_BODY
    finally:
        if _SCRIPTS_SRC in sys.path:
            sys.path.remove(_SCRIPTS_SRC)
    from rag_search.daemon.global_prompt import _PROMPT

    assert _PROMPT.strip() == CANONICAL_BODY.strip(), (
        "daemon/global_prompt.py::_PROMPT has drifted from canonical.CANONICAL_BODY — the "
        "MCP server is no longer serving what configure_integrations.py assumes it serves"
    )

    claude_md = Path.home() / ".claude" / "CLAUDE.md"
    assert claude_md.exists(), (
        "~/.claude/CLAUDE.md not found — Claude Code CLI must be installed in live+GPU environment"
    )
    text = claude_md.read_text()
    assert CANONICAL_BODY.strip() not in text, (
        "CANONICAL_BODY is duplicated in ~/.claude/CLAUDE.md — the MCP server already serves "
        "it. Run: .venv/bin/python scripts/configure_integrations.py --apply-all"
    )


def test_profile_discovery_targets_numbered_dirs(tmp_path):
    """_build_targets() must discover ~/.claude-1, ~/.claude-2, ... (real numbered
    profile dirs), and must NOT pick up ~/.claude-shared (the profiles' shared
    symlink target) or a ~/.claude-migration-backup-* dir, even though both
    names start with the same ".claude-" prefix.

    Regression test for the 2026-07-06 bug where discovery targeted the
    defunct ".claude-account{idx}" naming and silently found nothing on a
    real machine using ".claude-1"/".claude-2".
    """
    home = tmp_path / "home"
    for name in (".claude", ".claude-1", ".claude-2"):
        d = home / name
        d.mkdir(parents=True)
        (d / "CLAUDE.md").write_text("profile\n")
        (d / "settings.json").write_text("{}\n")

    # Shared symlink target: has its own settings.json for real, must be excluded.
    shared = home / ".claude-shared"
    shared.mkdir()
    (shared / "settings.json").write_text("{}\n")

    # Dated migration backup: must be excluded even though numbered-looking.
    backup = home / ".claude-migration-backup-20260705-134458"
    backup.mkdir()
    (backup / "settings.json").write_text("{}\n")

    env = dict(os.environ)
    env["HOME"] = str(home)
    results = _run_check_json(env=env)

    tools = {r["tool"] for r in results}
    assert any(".claude-1" in t for t in tools), f"expected .claude-1 discovered, got tools={tools}"
    assert any(".claude-2" in t for t in tools), f"expected .claude-2 discovered, got tools={tools}"
    assert not any("shared" in t.lower() for t in tools), f"shared dir leaked into targets: {tools}"
    assert not any("backup" in t.lower() for t in tools), f"backup dir leaked into targets: {tools}"


def test_mcp_resolves_via_claude_json_not_settings_json():
    """Regression guard for the 2026-07-08 bug: settings.json only holds MCP *approval*
    keys (enableAllProjectMcpServers, ...), never server definitions, so an entry written
    there is silently invisible to Claude Code. The repair/verify path must go through the
    `claude mcp` CLI against .claude.json instead — never patch settings.json directly.
    """
    sys.path.insert(0, str(_REPO / "scripts"))
    try:
        import configure_integrations as ci
    finally:
        sys.path.remove(str(_REPO / "scripts"))
    import inspect
    src = inspect.getsource(ci._repair_claude_mcp) + inspect.getsource(ci._verify_claude_mcp)
    assert "settings.json" not in src, "MCP repair/verify must not reference settings.json"

    claude_bin = ci._claude_binary()
    assert claude_bin, "claude binary not on PATH — required for the live suite"
    result = subprocess.run(
        [claude_bin, "mcp", "get", "rag-search"],
        capture_output=True, text=True, timeout=15,
    )
    urls = [ln.split(":", 1)[1].strip() for ln in result.stdout.splitlines()
            if ln.strip().startswith("URL:")]
    # Exact, not substring: a local-scope `?project=` pin is a *different* URL that a
    # substring check accepts, which is how three profiles drifted while --check said 9/9.
    assert result.returncode == 0 and ci.CANONICAL_MCP_URL in urls, (
        "claude mcp get rag-search (main profile) did not resolve the canonical MCP entry "
        f"from .claude.json: {result.stdout}\n{result.stderr}"
    )
    _, mcp_targets = ci._build_targets()
    for kind, config_dir, label in mcp_targets:
        if kind != "claude_mcp":
            continue
        shadowed = ci._shadowing_local_mcp(config_dir)
        assert not shadowed, (
            f"profile {label}: local-scope rag-search shadows the canonical user-scope entry "
            f"in {shadowed} — `claude mcp remove rag-search --scope local` from each"
        )
