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


@pytest.fixture
def health_units(tmp_path):
    """The health check and its timer, written where systemd-analyze can read them."""
    service = tmp_path / systemd.HEALTH_NAME
    service.write_text(systemd.health_text("/usr/bin/true"))
    timer = tmp_path / systemd.HEALTH_TIMER
    timer.write_text(systemd.health_timer_text())
    return service, timer


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="no systemd-analyze")
def test_systemd_accepts_the_health_units(health_units):
    for path in health_units:
        out = subprocess.run(
            ["systemd-analyze", "--user", "verify", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert "Unknown key name" not in out.stderr, out.stderr
        assert out.returncode == 0, out.stderr


def test_the_health_alert_is_wired_where_systemd_reads_it(health_units):
    """Same failure as the daemon unit's: `OnFailure` under `[Service]` parses,
    warns, and never fires -- and a health check nothing pages on is a cron job."""
    head, _, service = health_units[0].read_text().partition("\n[Service]\n")
    assert "OnFailure=" in head
    assert "OnFailure=" not in service


def test_the_health_check_runs_no_more_often_than_a_sweep(health_units):
    """It decides "still failing" by comparing two consecutive runs. Check twice
    inside one sweep and nothing has had the chance to retry, so every failure
    reads as persistent and the alert is noise."""
    found = re.search(r"OnUnitActiveSec=(\d+)s", health_units[1].read_text())
    assert found, "no OnUnitActiveSec, so the timer fires once at boot and never again"
    assert int(found[1]) >= config.SWEEP_EVERY_S
