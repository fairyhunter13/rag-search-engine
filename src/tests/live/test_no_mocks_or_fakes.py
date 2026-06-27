"""P15.1: Blocking anti-mock guard — makes the 'no mocks / real integration' invariant mechanical.

Scans every *.py under src/tests/ and FAILS if:
  (a) any mock/fake/stub/patch symbol appears, OR
  (b) any test uses build_test_app (zero-tolerance: P15.2 migration is complete).

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

# ── (b) build_test_app allowlist ──────────────────────────────────────────────
# P15.2 migration complete: build_test_app() deleted from routes.py; allowlist is empty.
# Any future re-introduction of build_test_app in a test FAILS this guard immediately.
_BUILD_TEST_APP_ALLOWLIST: frozenset[str] = frozenset()

_BUILD_TEST_APP_RE = re.compile(r"\bbuild_test_app\b")
_DEF_RE = re.compile(r"^def (test_\w+)", re.MULTILINE)
_THIS_FILE = Path(__file__).stem


def _current_test_in_function(lines: list[str], lineno: int) -> str | None:
    """Return the nearest def test_* name at or before lineno (1-based)."""
    fn = None
    for i, line in enumerate(lines, 1):
        m = re.match(r"^def (test_\w+)", line)
        if m:
            fn = m.group(1)
        if i >= lineno:
            break
    return fn


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


def test_build_test_app_only_in_allowlist():
    """(b) build_test_app may only appear in tests listed in _BUILD_TEST_APP_ALLOWLIST."""
    tests_root = Path(__file__).parents[1]  # src/tests/
    violations: list[str] = []
    for py in sorted(tests_root.rglob("*.py")):
        if py.stem == _THIS_FILE:
            continue
        if not _BUILD_TEST_APP_RE.search(py.read_text(encoding="utf-8")):
            continue
        lines = py.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, 1):
            if _BUILD_TEST_APP_RE.search(line):
                fn = _current_test_in_function(lines, lineno)
                key = f"{py.stem}::{fn}" if fn else f"{py.stem}::?"
                if key not in _BUILD_TEST_APP_ALLOWLIST:
                    rel = py.relative_to(tests_root.parent)
                    violations.append(
                        f"{rel}:{lineno} [{key}]: {line.strip()[:80]}\n"
                        f"  → Add to _BUILD_TEST_APP_ALLOWLIST (permanent negative test)\n"
                        f"    OR convert to live_client at :8765 (P15.2 pattern)."
                    )
    assert not violations, (
        "build_test_app used outside allowlist (use live daemon at :8765 for happy paths):\n"
        + "\n".join(violations)
    )
