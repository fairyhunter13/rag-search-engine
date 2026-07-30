"""SE3-SE5: the daemon's crash notifier is wired to something, and it demonstrably fires.

Residue N2 from the 07-30 work. `rag-search-mcp-failure-notify.service` sat on this host for months
as a `static` unit that nothing could ever start: `unit_text()` emitted no `OnFailure=`, no drop-in
did either, and 18aca54 deleted the repo's copy (it had the wrong name) without putting one back.
With `Restart=always` against `StartLimitBurst=20` the daemon can exhaust its restarts, enter
`failed` and sit there — precisely the case the notifier was written for, and precisely when it was
unreachable.

SE5 is the load-bearing one. The plan's instruction was *prove it fires* — a notifier nobody has seen
fire is N2 again, and every string-level check here (SE3, SE4) was equally true of the broken state
in every respect except the one that mattered.
"""
from __future__ import annotations

import os
import re
import subprocess
import time

import pytest

pytestmark = pytest.mark.live


def _systemctl(*args: str) -> str:
    r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)
    return r.stdout if r.returncode == 0 else ""


def _loaded(unit: str) -> bool:
    return _systemctl("show", "-p", "LoadState", "--value", unit).strip() == "loaded"


def test_se3_the_unit_names_a_failure_handler_and_that_handler_exists():
    """SE3: `OnFailure=` is emitted, and the unit it names is one systemd can actually load.

    Both halves, because either alone is a bug that has been here. An `OnFailure=` pointing at a unit
    that does not exist is not reported at load time — systemd discovers it when the daemon fails,
    the one moment nobody is positioned to notice — and a notify unit with no `OnFailure=` anywhere
    pointing at it is the state this residue is named for.
    """
    from rag_search.daemon.systemd import NOTIFY_UNIT, UNIT_NAME, unit_text

    assert f"OnFailure={NOTIFY_UNIT}" in unit_text("/usr/bin/rag-search"), (
        "the generated unit emits no OnFailure= — the notifier can never be activated")
    assert _loaded(UNIT_NAME), (
        f"{UNIT_NAME} is not loaded, yet the live suite just reached a daemon it starts — the "
        "host is in a state this test cannot interpret, which is a failure, not a pass")
    assert _loaded(NOTIFY_UNIT), (
        f"{NOTIFY_UNIT} is not loadable, so OnFailure= names a unit that is not there")


def test_se4_the_deployed_notifier_is_the_one_the_repo_generates():
    """SE4: the notifier ships with the unit that references it, and the host copy is that one.

    The generator is the versioned form — `install()` writes both units from it — so this is what
    stops the only working notifier from living on a single machine with nothing describing it.
    """
    from rag_search.daemon.systemd import NOTIFY_UNIT, UNIT_NAME, notify_unit_text

    generated = notify_unit_text()
    assert f"is-active {UNIT_NAME}" in generated, (
        "the notifier does not re-check the daemon before notifying, so it would page for crashes "
        "that recovered before the popup rendered — a critical alert that is usually wrong gets "
        "dismissed reflexively, which is the same as not having one")

    live = os.path.expanduser(f"~/.config/systemd/user/{NOTIFY_UNIT}")
    assert os.path.exists(live), (
        f"{NOTIFY_UNIT} is not deployed to {live} — SE3 requires the daemon's OnFailure= to name a "
        "loadable unit, so a missing one is the residue this file exists to close, not a skip")
    with open(live) as fh:
        assert fh.read() == generated, (
            f"the deployed {NOTIFY_UNIT} differs from what install() writes — the host was edited "
            "by hand, or a redeploy is owed")


def test_se5_onfailure_actually_activates_the_notifier():
    """SE5: proven by making a real unit fail, not by finding the word `OnFailure` in a file.

    A throwaway transient unit is wired to the *same* notify unit and exits non-zero. The daemon is
    never touched — the wiring is what is under test, and the notify script's first act is to ask
    whether the daemon is up, which it is, so it exits silently. That is the difference between
    proving the mechanism and staging an outage to watch a popup.

    Two systemd details, both measured here rather than assumed, because the obvious form of this
    test can never pass:

    - **`ActiveEnterTimestamp` is the wrong signal.** A `Type=oneshot` without `RemainAfterExit`
      goes `inactive -> activating -> inactive` and never passes through `active`, so that timestamp
      stays `0` however many times the notifier runs. `InactiveExitTimestampMonotonic` is what marks
      "systemd started this unit", which is the claim.
    - **The reading must happen inside the window.** Once the unit is inactive and unreferenced
      systemd garbage-collects its state and both timestamps go back to `0` — so a value sampled
      after cleanup reports a notifier that did fire as one that never did.
    """
    from rag_search.daemon.systemd import NOTIFY_UNIT

    assert _loaded(NOTIFY_UNIT), (
        f"{NOTIFY_UNIT} is not loaded, so the wiring SE3 asserts in the generated unit points at "
        "nothing on the host that runs it — the one state this test must never report as a pass")

    def started_at() -> str:
        return _systemctl(
            "show", "-p", "InactiveExitTimestampMonotonic", "--value", NOTIFY_UNIT).strip()

    before = started_at()
    probe = f"rse-onfailure-probe-{os.getpid()}"
    try:
        started = subprocess.run(
            ["systemd-run", "--user", f"--unit={probe}", "-p", f"OnFailure={NOTIFY_UNIT}",
             "-p", "Type=oneshot", "--", "/bin/false"],
            capture_output=True, text=True, timeout=30)
        # A non-zero exit is the *expected* path — systemd-run waits for the oneshot and reports the
        # failure it was asked to produce. So "did the probe run" is asked of systemd, not of the
        # exit code, which cannot tell a working probe from a host that refused to start one.
        assert _loaded(probe), (
            f"systemd-run could not start the transient probe: {started.stderr.strip()!r}. Without "
            "it nothing fails, so the notifier is never activated and the assertion below would be "
            "measuring the probe's absence rather than the wiring")
        # Sampled during the notifier's own `sleep 8`, before the cleanup below can collect it away.
        deadline = time.monotonic() + 25
        after = started_at()
        while time.monotonic() < deadline and after == before:
            time.sleep(0.5)
            after = started_at()
    finally:
        subprocess.run(["systemctl", "--user", "reset-failed", probe],
                       capture_output=True, text=True, timeout=30)
        subprocess.run(["systemctl", "--user", "stop", NOTIFY_UNIT],
                       capture_output=True, text=True, timeout=30)

    assert after != before and re.fullmatch(r"[1-9]\d*", after or ""), (
        f"a unit carrying OnFailure={NOTIFY_UNIT} failed and systemd never started the notifier "
        f"(InactiveExitTimestampMonotonic went {before!r} -> {after!r}) — the wiring does not work")
