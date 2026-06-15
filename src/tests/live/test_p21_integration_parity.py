"""P21: Integration-parity tests — 7-tree config drift guard.

Puts the existing 7-tree integration system under the live test suite for the first time.
Reuses configure_integrations.py --check --json rather than reimplementing the logic.
Skips gracefully if a target's config file does not exist (tool not installed).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_REPO = Path(__file__).resolve().parent.parent.parent.parent
_SCRIPT = _REPO / "scripts" / "configure_integrations.py"
_SCRIPTS_SRC = str(_REPO / "scripts")


def _run_check_json() -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--check", "--json"],
        capture_output=True, text=True, timeout=30,
        cwd=str(_REPO),
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"configure_integrations --check --json produced non-JSON output "
            f"(exit {result.returncode}):\n{result.stdout[:500]}\n{result.stderr[:200]}"
        ) from exc


def test_integration_check_exits_zero():
    """configure_integrations.py --check exits 0 — all installed trees canonical-in-sync."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--check", "--json"],
        capture_output=True, text=True, timeout=30,
        cwd=str(_REPO),
    )
    assert result.returncode == 0, (
        f"configure_integrations --check exited {result.returncode}\n"
        f"stdout:\n{result.stdout[-2000:]}\n"
        f"stderr:\n{result.stderr[-300:]}"
    )


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
    if not claude_md.exists():
        pytest.skip("~/.claude/CLAUDE.md not found — claude not installed")
    text = claude_md.read_text()
    assert CANONICAL_BODY.strip() in text, (
        "CANONICAL_BODY not found verbatim in ~/.claude/CLAUDE.md — drift detected"
    )
