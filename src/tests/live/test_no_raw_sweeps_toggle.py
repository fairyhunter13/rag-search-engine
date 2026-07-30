"""Guard: no live test may toggle sweeps without restoring the state it found.

`daemon/sweeps.py:10` `_PAUSED` is a bare global with no nesting or ownership, and conftest's
session-scoped autouse `pause_sweeps` holds it True for the whole run (GPU contention, HR41).
A test that hardcodes its restore is therefore asserting something about its environment:
`test_cpu_budget.py`'s CB3 paused and then unconditionally *resumed*, and because that file
sorts 7th of 76, ~70 files after it ran against a sweeping daemon. Nothing failed at the
toggle — it surfaced as flakiness elsewhere, naming neither sweeps nor the GPU, which is
exactly why a static guard is worth more here than a runtime one.

Three invariants, same shape as test_no_unbounded_parse.py:
1. only `_sweeps.py` may name the pause/resume routes — everyone else goes through
   `sweeps_state`, which restores what it read;
2. any file assigning `sweeps._PAUSED` must import `local_sweeps_paused` — this permits the
   deliberate mid-loop flip in test_reconcile_midpass.py while still requiring the file to be
   wrapped in the restoring CM;
3. the allowlist entry must still describe something real, so a refactor of `_sweeps.py` fails
   loudly rather than leaving the guard scanning for text that no longer exists.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_LIVE_DIR = Path(__file__).resolve().parent
_HELPER = "_sweeps.py"
# This file names the routes in order to ban them, so it exempts itself alongside the helper.
_EXEMPT = {_HELPER, Path(__file__).name}

_ROUTE_PREFIX = "/api/sweeps/"

# `sweeps._PAUSED = ...`, `sweeps_mod._PAUSED = ...` or a bare `_PAUSED = ...`, but not a
# comparison, so `if _PAUSED ==` is left alone.
_PAUSED_ASSIGN_RE = re.compile(r"^\s*(?:\w+\.)?_PAUSED\s*=(?!=)", re.MULTILINE)


def _live_py_files() -> list[Path]:
    return sorted(p for p in _LIVE_DIR.glob("*.py") if p.name not in _EXEMPT)


def _route_strings(src: str) -> list[str]:
    """String constants naming the route, ignoring docstrings and comments.

    Mentioning a route is not calling one: test_http_surface.py and test_reconcile_midpass.py
    both describe `/api/sweeps/*` in prose, and a guard that fires on prose gets weakened
    until it stops guarding. Comments never reach the AST; docstrings are skipped explicitly.
    """
    tree = ast.parse(src)
    docstrings = {
        ast.get_docstring(n, clean=False)
        for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    return [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and _ROUTE_PREFIX in n.value and n.value not in docstrings
    ]


def test_no_raw_sweeps_route_calls_outside_helper() -> None:
    violations: list[str] = []
    for py in _live_py_files():
        named = _route_strings(py.read_text(errors="replace"))
        if named:
            violations.append(f"{py.name}: names {named}")
    assert not violations, (
        "Live tests must not POST the sweeps pause/resume routes directly — use "
        "`tests.live._sweeps.sweeps_state`, which restores the state it found instead of a "
        "hardcoded one:\n" + "\n".join(violations)
    )


def test_paused_assignments_go_through_the_restoring_helper() -> None:
    violations: list[str] = []
    for py in _live_py_files():
        src = py.read_text(errors="replace")
        if _PAUSED_ASSIGN_RE.search(src) and "local_sweeps_paused" not in src:
            violations.append(py.name)
    assert not violations, (
        "Assigns sweeps._PAUSED without importing `tests.live._sweeps.local_sweeps_paused` — "
        "the in-process pause must be wrapped in the CM that restores what it read:\n"
        + "\n".join(violations)
    )


def test_helper_still_describes_something_real() -> None:
    """Allowlist accuracy: `_sweeps.py` must still contain what the guards exempt it for."""
    src = (_LIVE_DIR / _HELPER).read_text(errors="replace")
    assert _ROUTE_PREFIX in src, (
        f"{_HELPER} is the sole allowlisted caller of {_ROUTE_PREFIX}* but no longer builds "
        "that route — the first guard now bans a call nothing is left to make legitimately"
    )
    assert "previously_paused" in src, (
        f"{_HELPER} no longer reads `previously_paused`, so it cannot be restoring the state "
        "it found — the whole point of routing callers through it"
    )
    assert _PAUSED_ASSIGN_RE.search(src), (
        f"{_HELPER} no longer assigns _PAUSED — `local_sweeps_paused` is gone or renamed, so "
        "the second guard's import requirement points at nothing"
    )
