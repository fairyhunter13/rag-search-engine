"""Public-repo hygiene guard: removed env-flag names and absolute home paths must not reappear.

Checks removed OSE flag names (renames that would be breaking) and generic home-path patterns.
Device-specific name bans (company names, project codenames, device ids) live in the private
ose-live-audit repo to avoid shipping the ban-list in the public tree.

Runs git grep (case-insensitive) over the tracked tree and fails on any match.
Guard file and .gitmodules are allowlisted automatically.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_REPO_ROOT = Path(__file__).parents[3]
_THIS_FILE = Path(__file__).name

# Removed OSE flag names that must not reappear (would break existing deployments if re-added
# under the old name after they were intentionally removed/renamed).
_FORBIDDEN_FLAGS = [
    "OSE_WIKI_LLM",
    "OSE_BPRE_LLM_LINK",
    "OSE_BPRE_LLM_FILE",
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


@pytest.mark.parametrize("token", _FORBIDDEN_FLAGS)
def test_removed_flag_absent(token: str) -> None:
    """Removed OSE flag name must not reappear in any tracked file (case-insensitive)."""
    hits = _git_grep(token)
    assert not hits, (
        f"Removed flag {token!r} found in tracked files "
        f"({len(hits)} occurrence(s)):\n" + "\n".join(hits[:5])
    )


def _git_grep_re(pattern: str, exclude_file: str) -> list[str]:
    result = subprocess.run(
        [
            "git", "grep", "-nE", pattern,
            "--",
            ".",
            ":(exclude).gitmodules",
            f":(exclude){exclude_file}",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return [ln for ln in result.stdout.splitlines() if ln.strip()]


def test_no_absolute_home_paths() -> None:
    """Absolute /home/<user>/, /root/<path>/, /Users/<user>/, or Windows C:\\Users\\<user>\\
    paths must not appear anywhere in the tracked tree (source, tests, docs, scripts,
    generated artifacts)."""
    hits = _git_grep_re(
        r"/home/[a-zA-Z0-9_.-]+/|/root/[a-zA-Z0-9_.-]+/|/Users/[a-zA-Z0-9_.-]+/|C:\\\\Users\\\\[a-zA-Z0-9_.-]+",
        f"src/tests/live/{_THIS_FILE}",
    )
    assert not hits, (
        f"Absolute home paths found in tracked files ({len(hits)} occurrence(s)):\n"
        + "\n".join(hits[:5])
    )


_PATH_LITERAL_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*\s*(?::\s*\S+)?\s*=\s*Path\((.*)\)\s*$')


def test_storage_paths_are_env_driven() -> None:
    """Module-level Path(...) storage-root constants in core/ and daemon/ must derive from
    os.environ.get(...) (XDG_DATA_HOME / OPENCODE_*) — never a hardcoded machine-specific literal
    (P18/HR34 device-neutrality). Paths built from other already-derived names (e.g. REGISTRY_PATH)
    are allowed."""
    targets = [
        _REPO_ROOT / "src/opencode_search/core/config.py",
        _REPO_ROOT / "src/opencode_search/core/registry.py",
        *sorted((_REPO_ROOT / "src/opencode_search/daemon").glob("*.py")),
    ]
    violations: list[str] = []
    for f in targets:
        if not f.exists():
            continue
        for lineno, line in enumerate(f.read_text().splitlines(), start=1):
            m = _PATH_LITERAL_RE.match(line.strip())
            if not m:
                continue
            inner = m.group(1)
            if "os.environ.get(" in inner:
                continue
            if re.search(r'"[^"]*/[^"]*"|\'[^\']*/[^\']*\'', inner):
                violations.append(f"{f.relative_to(_REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not violations, (
        f"Hardcoded storage-root literal(s) found ({len(violations)}):\n" + "\n".join(violations[:5])
    )
