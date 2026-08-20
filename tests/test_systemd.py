"""The unit text, checked by the thing that will actually read it.

systemd does not fail on a key it does not know -- it logs `Unknown key name`
and carries on. So a misspelled or misplaced directive is a setting that is
silently absent, which is exactly how `OnFailure` in `[Service]` became an
alert that never fired and never said it had not. Every test here asserts
against `systemd-analyze --user verify` or against an ordering that has a
number behind it, never against the string being present.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from coderag import config, systemd


@pytest.fixture
def unit(tmp_path):
    """The unit as installed, written where systemd-analyze can read it."""
    path = tmp_path / systemd.UNIT_NAME
    path.write_text(systemd.unit_text("/usr/bin/true"))
    (tmp_path / systemd.ALERT_NAME).write_text(systemd.alert_text())
    return path


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="no systemd-analyze")
def test_systemd_itself_accepts_every_key(unit):
    """The discriminator is stderr, not the exit code: `Unknown key name` is a
    warning, so a unit full of typos verifies clean by return code alone."""
    out = subprocess.run(
        ["systemd-analyze", "--user", "verify", str(unit)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "Unknown key name" not in out.stderr, out.stderr
    assert out.returncode == 0, out.stderr


def test_the_systemd_timeout_is_strictly_later_than_our_own_deadline(unit):
    """Backstop, not the mechanism. If systemd fires first the process is
    SIGKILLed, which `Restart=on-failure` reads as a failure and pages for --
    the outage the shutdown deadline was written to end."""
    found = re.search(r"TimeoutStopSec=(\d+)", unit.read_text())
    assert found, "no TimeoutStopSec, so systemd's 90 s default applies"
    assert int(found[1]) > config.SHUTDOWN_DEADLINE_S


def test_the_alert_is_wired_where_systemd_reads_it(unit):
    """`OnFailure` in `[Service]` is the failure mode this asserts against: it
    parses, it warns, and the alert never fires."""
    head, _, service = unit.read_text().partition("\n[Service]\n")
    assert "OnFailure=" in head
    assert "OnFailure=" not in service
