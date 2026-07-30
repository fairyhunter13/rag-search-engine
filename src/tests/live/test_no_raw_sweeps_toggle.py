"""Guard: no live test may toggle sweeps without restoring the state it found.

`daemon/sweeps.py:10` `_PAUSED` is a bare global with no nesting or ownership, and conftest's
session-scoped autouse `pause_sweeps` holds it True for the whole run (GPU contention, HR41).
A test that hardcodes its restore is therefore asserting something about its environment:
`test_cpu_budget.py`'s CB3 paused and then unconditionally *resumed*, and because that file
sorts 7th of 76, ~70 files after it ran against a sweeping daemon. Nothing failed at the
toggle — it surfaced as flakiness elsewhere, naming neither sweeps nor the GPU, which is
exactly why a static guard is worth more here than a runtime one.

Five invariants, same shape as test_no_unbounded_parse.py — the last two extend the same rule to
production code, where a raw assignment now also skips the pause stamp /healthz publishes:
1. only `_sweeps.py` may name the pause/resume routes — everyone else goes through
   `sweeps_state`, which restores what it read;
2. any file assigning `sweeps._PAUSED` must import `local_sweeps_paused` — this permits the
   deliberate mid-loop flip in test_reconcile_midpass.py while still requiring the file to be
   wrapped in the restoring CM;
3. the allowlist entry must still describe something real, so a refactor of `_sweeps.py` fails
   loudly rather than leaving the guard scanning for text that no longer exists;
4. no file under `rag_search/` may assign `_PAUSED` except `sweeps.py`, which owns it — everyone
   else calls `sweeps.set_paused`, the only path that stamps when the pause began;
5. `sweeps.py` must still be that writer and still keep the stamp, for the same reason as (3).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_LIVE_DIR = Path(__file__).resolve().parent
_SRC_DIR = _LIVE_DIR.parents[1] / "rag_search"
_OWNER = "sweeps.py"  # the only production file allowed to assign the flag it owns
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


def test_production_pauses_only_through_the_stamping_setter() -> None:
    """`sweeps.set_paused` is the sole writer in `rag_search/`, so the pause stamp cannot be bypassed.

    `paused_seconds()` — what /healthz publishes as `sweeps_paused_s` — reads `_PAUSED_SINCE` and
    does not repair a missing stamp, because a reader that invents one reports 0.0 on the first poll
    of a real pause, which is the blind spot rather than a fix for it. That is only sound while
    every production write goes through `set_paused`, and the write it replaced was a bare
    `sweeps._PAUSED = paused` in `server/routes_ops.py`. A second route or CLI path added the same
    way would be silently invisible to /healthz, so the constraint is asserted rather than assumed
    ([[feedback_allowlist_needs_sufficiency_test]]: delete the setter call and this must go red).
    """
    violations = [
        str(py.relative_to(_SRC_DIR)) for py in sorted(_SRC_DIR.rglob("*.py"))
        if py.name != _OWNER and _PAUSED_ASSIGN_RE.search(py.read_text(errors="replace"))
    ]
    assert not violations, (
        "Assigns sweeps._PAUSED directly instead of calling `sweeps.set_paused()` — the pause "
        "would not be stamped, so /healthz `sweeps_paused_s` would read 0.0 while paused:\n"
        + "\n".join(violations)
    )


def test_owner_still_assigns_and_stamps() -> None:
    """Allowlist accuracy for `_OWNER`: the exemption must still describe a real writer."""
    src = (_SRC_DIR / "daemon" / _OWNER).read_text(errors="replace")
    assert _PAUSED_ASSIGN_RE.search(src), (
        f"{_OWNER} is exempted as the sole production writer of _PAUSED but no longer assigns it — "
        "the guard above now bans a write nothing is left to make legitimately"
    )
    assert "_PAUSED_SINCE" in src and "def paused_seconds" in src, (
        f"{_OWNER} no longer keeps the pause stamp, so /healthz `sweeps_paused_s` cannot be "
        "reporting how long sweeps have been paused — the guard's whole purpose"
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
