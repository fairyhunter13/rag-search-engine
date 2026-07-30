"""SC1-SC2: the live suite refuses to run beside another live suite in this checkout.

Two suites share one 1-core daemon cgroup, one GPU, one registry and one global sweep pause, so an
overlap does not show up as slowness — it shows up as wrong measurements. The 2026-07-30 overlap
produced CB3 reading 0.44 core on an "idle" daemon, a 5 s `/api/metrics` timeout, 106 pause calls
against 4 resumes, two leaked store sets and 11 session-setup errors that vanished on re-run.

Both directions are asserted because the detector has already been wrong in both: it first reported
its own wrapper shell and `timeout` parent as contenders (they carry "pytest" in their command
line), which would refuse every run, and a substring test on the command line would also latch onto
a wrapper shell that outlived its pytest and block runs forever. No daemon and no GPU: this drives
`_contending_live_runs` against a real process it spawns.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.live.conftest import _contending_live_runs

pytestmark = pytest.mark.live

_REPO = Path(__file__).parents[3]


def test_sc1_no_contender_is_reported_for_our_own_process_tree():
    """SC1: the run must not see itself. This is the assertion the first version failed."""
    assert _contending_live_runs() == [], (
        "the detector reported a contender while only this run is active — most likely it is "
        "matching our own wrapper shell or `timeout` parent, whose command lines contain the word "
        "'pytest'. That refuses every run, which is worse than the collision it prevents."
    )


def test_sc2_a_real_pytest_process_in_this_checkout_is_reported():
    """SC2: a live pytest process in this repo is found, named by pid and profile.

    argv is shaped the way a real run looks — an interpreter whose arguments include something
    whose basename is `pytest` — rather than a shell whose command line merely mentions it.
    """
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "/fake/bin/pytest"],
        cwd=str(_REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:  # /proc/<pid>/cmdline is not populated instantly
            found = _contending_live_runs()
            if any(f" pid {child.pid} " in f for f in found):
                return
            time.sleep(0.2)
        pytest.fail(
            f"a pytest process (pid {child.pid}) running in {_REPO} was not reported: "
            f"{_contending_live_runs()}"
        )
    finally:
        child.kill()
        child.wait(timeout=10)
