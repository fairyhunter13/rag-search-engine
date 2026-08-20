"""The knowledge bundle, and the gate that decides on it.

Fail-closed on a missing checker, deliberately: if a missing `okfrules` binary made
this skip, the bundle's two possible outcomes would be pass and silence, which is the
same thing. The last test asks the question the others cannot -- whether anything runs
this file at all.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BUNDLE = REPO / "knowledge"
CI = REPO / ".github" / "workflows" / "ci.yml"

# Pinned: `@latest` lets the verdict change with no commit in this repo.
INSTALL = "go install github.com/fairyhunter13/okfrules/cmd/okfrules@v0.2.1"


def test_the_checker_is_installed():
    """Named separately so a missing binary reads as a missing tool, not as a
    broken bundle."""
    assert shutil.which("okfrules"), f"okfrules is not on PATH; the bundle is ungated. {INSTALL}"


def test_the_bundle_passes_okf_check():
    okf = shutil.which("okfrules")
    if okf is None:
        pytest.fail(f"okfrules is not on PATH; the bundle is ungated. {INSTALL}")
    out = subprocess.run([okf, "check", str(BUNDLE)], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stdout + out.stderr


def test_the_checker_actually_rejects_something(tmp_path):
    """Without this, the two tests above pass just as well against a checker
    that exits 0 on everything and a bundle directory that is empty."""
    okf = shutil.which("okfrules")
    if okf is None:
        pytest.fail(f"okfrules is not on PATH; the bundle is ungated. {INSTALL}")
    bad = tmp_path / "knowledge"
    bad.mkdir()
    (bad / "index.md").write_text("no frontmatter, no okf_version\n")
    (bad / "broken.md").write_text("---\ntitle: no type key\n---\n\nbody\n")
    out = subprocess.run([okf, "check", str(bad)], capture_output=True, text=True, check=False)
    assert out.returncode != 0, "okfrules accepted a bundle with no type key"


def test_the_gate_is_wired_where_this_repo_says_it_is():
    """This file is only a gate if something runs it.

    Everything above grades the bundle; none of it notices a CI job that stopped
    invoking these tests, or a step whose failure is excused. Both leave the same green
    as a clean bundle.
    """
    ci = CI.read_text()
    assert "pytest tests/test_okf_bundle.py" in ci, "no CI step runs the bundle tests"
    assert "okfrules check -Werror knowledge" in ci, "no CI step runs the strict check"
    assert INSTALL in ci, f"CI does not install the pinned checker: {INSTALL}"
    # The key itself, not the word: one of these steps carries a comment saying it is
    # deliberately not continue-on-error, and matching that would grade the prose.
    excused = [ln.strip() for ln in ci.splitlines() if ln.strip().startswith("continue-on-error:")]
    assert excused == [], f"a step whose failure is excused reports a passing green: {excused}"
    # A workflow with no trigger that fires on a change grades the calendar instead.
    assert "push:" in ci or "pull_request:" in ci, "no CI trigger fires on a change"
