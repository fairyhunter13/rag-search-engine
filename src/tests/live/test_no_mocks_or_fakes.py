"""P15.1: Blocking anti-mock guard — makes the 'no mocks / real integration' invariant mechanical.

Scans every *.py under src/tests/ and FAILS if:
  (a) any mock/fake/stub/patch symbol appears, OR
  (b) any test uses build_test_app outside the named allowlist below.

The allowlist distinguishes:
  - PERMANENT negative/error-injection uses (4xx, empty-project): build_test_app is
    justified here because we deliberately inject bad inputs the live daemon won't accept.
  - PENDING P15.2 conversion: happy-path uses that must be migrated to live_client at :8765.
    Removing them from the allowlist is the proof of P15.2 completion.

This test itself is excluded from the build_test_app allowlist check.
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
# Key = "module_stem::test_function_name"
# PERMANENT: negative/error injection — build_test_app is justified (no live daemon needed)
# PENDING P15.2: happy-path tests awaiting conversion to live_client at :8765
_BUILD_TEST_APP_ALLOWLIST = {
    # PERMANENT — deliberate 4xx / empty-data injection:
    "test_p5_server::test_api_search_missing_query_returns_400",
    "test_p5_server::test_api_search_nonexistent_project_returns_empty",
    "test_p5_server::test_api_index_missing_path_returns_400",
    # PENDING P15.2 — happy-path, must migrate to live_client:
    "test_p5_server::test_healthz",
    "test_p5_server::test_dashboard_five_views",
    "test_p5_server::test_api_projects_returns_list",
    "test_p5_server::test_api_overview_projects",
    "test_p4_query::test_chat_stream_sse_sends_done",
}

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
