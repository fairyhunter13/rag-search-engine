"""P21: Integration-parity tests — config drift guard.

Puts the existing integration system (claude profiles + hermes; OpenCode and
Codex are no longer configured) under the live test suite.
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


def test_canonical_body_in_main_claude_md():
    """drift guard: canonical.CANONICAL_BODY must be present in the main ~/.claude/CLAUDE.md."""
    sys.path.insert(0, _SCRIPTS_SRC)
    try:
        from integrations.canonical import CANONICAL_BODY
    finally:
        if _SCRIPTS_SRC in sys.path:
            sys.path.remove(_SCRIPTS_SRC)

    claude_md = Path.home() / ".claude" / "CLAUDE.md"
    assert claude_md.exists(), (
        "~/.claude/CLAUDE.md not found — Claude Code CLI must be installed in live+GPU environment"
    )
    text = claude_md.read_text()
    assert CANONICAL_BODY.strip() in text, (
        "CANONICAL_BODY not found verbatim in ~/.claude/CLAUDE.md — drift detected"
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
