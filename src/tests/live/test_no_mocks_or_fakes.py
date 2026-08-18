"""Blocking anti-mock guard — makes the 'no mocks / real integration' invariant mechanical.

Scans every *.py under src/tests/ and FAILS if:
  (a) any mock/fake/stub/patch symbol appears, OR
  (b) any test uses build_test_app (zero-tolerance: the migration is complete).

All tests must drive real integrations through live_client at :8765 or direct MCP tool imports.
build_test_app() has been deleted from routes.py; any attempt to re-introduce it is caught here.

This test itself is excluded from the build_test_app scan.
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

# ── (a) Forbidden mock/fake/stub/patch patterns ────────────────────────────────
_MOCK_PATTERNS = [
    r"\bunittest\.mock\b",
    r"\bMagicMock\b",
    r"\bMock\s*\(",
    r"(?:^|\s)@patch\b",
    r"\bpatch\s*\(",
    r"\bmonkeypatch\b",
    r"\bmocker\b",
    r"\bresponses\b",           # responses library (HTTP mocking)
    r"\bhttpretty\b",
    r"\.stub\s*\(",
    r"\bfake_\w+\s*=",
    r"\bdummy_\w+\s*=",
]
_MOCK_RE = re.compile("|".join(_MOCK_PATTERNS))

_BUILD_TEST_APP_RE = re.compile(r"\bbuild_test_app\b")
_THIS_FILE = Path(__file__).stem


def test_no_mocks_or_fakes():
    """(a) Zero mock/fake/stub/patch symbols anywhere in src/tests/."""
    tests_root = Path(__file__).parents[1]  # src/tests/
    violations: list[str] = []
    for py in sorted(tests_root.rglob("*.py")):
        if py.stem == _THIS_FILE:
            continue
        text = py.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if _MOCK_RE.search(line):
                rel = py.relative_to(tests_root.parent)
                violations.append(f"{rel}:{lineno}: {line.strip()[:80]}")
    assert not violations, (
        "Mock/fake/stub/patch found in tests (forbidden — use real integration):\n"
        + "\n".join(violations)
    )


def test_build_test_app_is_gone():
    """(b) build_test_app() was deleted from routes.py; a re-introduction fails here.

    There is no allowlist. The escape hatch was an empty frozenset for months, and an exemption
    list nobody can be exempted by is a shape that invites the first entry.
    """
    tests_root = Path(__file__).parents[1]  # src/tests/
    violations: list[str] = []
    for py in sorted(tests_root.rglob("*.py")):
        if py.stem == _THIS_FILE:
            continue
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if _BUILD_TEST_APP_RE.search(line):
                violations.append(f"{py.relative_to(tests_root.parent)}:{lineno}: {line.strip()[:80]}")
    assert not violations, (
        "build_test_app is deleted — use the live daemon at :8765 (the live_client pattern):\n"
        + "\n".join(violations)
    )
