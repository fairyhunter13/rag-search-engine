"""Public-repo hygiene guard: forbidden tokens and removed flag names must not reappear.

Runs git grep (case-insensitive) over the tracked tree and fails if any forbidden string is found.
Also scans tracked filenames via git ls-files. Guard file and .gitmodules are allowlisted.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_REPO_ROOT = Path(__file__).parents[3]
_THIS_FILE = Path(__file__).name

_FORBIDDEN = [
    "/home/redacted-name-11",
    "astronautsid",
    "jerman-corp",
    "redacted-name-7",
    "redacted-name-10",
    "redacted-name-3",
    "acme-promo",
    "acme-campaign",
    "redacted-name-0",
    "RTX 5080",
    "OSE_WIKI_LLM",
    "OSE_BPRE_LLM_LINK",
    "OSE_BPRE_LLM_FILE",
    # bare codename stem + fleet prefixes not already covered
    "astro",
    "gen2",
    "web-tree",
    "gen3-app-",
    "go-monorepo",
    "ts-gradio",
]


def _git_grep(pattern: str) -> list[str]:
    result = subprocess.run(
        [
            "git", "grep", "-niF", pattern,
            "--",
            ".",
            ":(exclude).gitmodules",
            f":(exclude)src/tests/live/{_THIS_FILE}",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return [ln for ln in result.stdout.splitlines() if ln.strip()]


@pytest.mark.parametrize("token", _FORBIDDEN)
def test_forbidden_token_absent(token: str) -> None:
    """Forbidden token must not appear in any tracked file (case-insensitive)."""
    hits = _git_grep(token)
    assert not hits, (
        f"Forbidden token {token!r} found in tracked files "
        f"({len(hits)} occurrence(s)):\n" + "\n".join(hits[:5])
    )


_FILENAME_FORBIDDEN = ["astro", "redacted-name-10", "redacted-name-7", "jerman", "gen2", "web-tree", "gen3-app"]


def _git_ls_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


@pytest.mark.parametrize("stem", _FILENAME_FORBIDDEN)
def test_forbidden_filename_absent(stem: str) -> None:
    """Forbidden stem must not appear in any tracked filename."""
    files = _git_ls_files()
    hits = [f for f in files if stem in f.lower() and _THIS_FILE not in f]
    assert not hits, (
        f"Forbidden stem {stem!r} found in tracked filenames "
        f"({len(hits)} file(s)):\n" + "\n".join(hits[:5])
    )
