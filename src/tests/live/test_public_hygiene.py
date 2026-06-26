"""Public-repo hygiene guard: forbidden tokens and removed flag names must not reappear.

Runs git grep over the tracked tree and fails if any forbidden string is found.
The guard file itself and .gitmodules are allowlisted.
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
]


def _git_grep(pattern: str) -> list[str]:
    result = subprocess.run(
        [
            "git", "grep", "-nF", pattern,
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
    """Forbidden token must not appear in any tracked file."""
    hits = _git_grep(token)
    assert not hits, (
        f"Forbidden token {token!r} found in tracked files "
        f"({len(hits)} occurrence(s)):\n" + "\n".join(hits[:5])
    )
