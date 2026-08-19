"""The thermal governor, with the clock and the sensor both injected.

Written after measuring this laptop mid-index: 89 C, `GPU T.Limit` at -3 --
three degrees past the card's own throttle point -- and an SM clock of 460 MHz
against a rated 3090. The card was doing about a sixth of its work rate, which
is a larger effect than any model on the shortlist.
"""

from __future__ import annotations

import pytest

from coderag import config, gpu


@pytest.fixture
def slept(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(config, "INDEX_TEMP_C", 84)
    monkeypatch.setattr(config, "INDEX_TEMP_POLL_S", 5)
    monkeypatch.setattr(config, "INDEX_TEMP_WAIT_S", 20)
    return calls


def _at(monkeypatch, *readings: int):
    """A sensor that walks a script and then holds its last value."""
    seq = list(readings)
    monkeypatch.setattr(gpu, "gpu_temp_c", lambda: seq.pop(0) if len(seq) > 1 else seq[0])


def test_a_cool_card_is_never_waited_on(monkeypatch, slept):
    _at(monkeypatch, 60)
    assert gpu.cool_down(sleep=slept.append) == 0.0
    assert slept == []


def test_a_hot_card_is_waited_on_until_it_cools(monkeypatch, slept):
    _at(monkeypatch, 89, 88, 85, 83)
    assert gpu.cool_down(sleep=slept.append) == 15.0
    assert slept == [5, 5, 5]


def test_the_wait_is_bounded_when_it_never_cools(monkeypatch, slept):
    """A card that stays hot -- the laptop's actual steady state -- must not
    stall the queue forever. Indexing slowly beats not indexing."""
    _at(monkeypatch, 89)
    assert gpu.cool_down(sleep=slept.append) == 20.0


def test_the_governor_is_off_by_default(monkeypatch, slept):
    monkeypatch.setattr(config, "INDEX_TEMP_C", 0)
    _at(monkeypatch, 99)
    assert gpu.cool_down(sleep=slept.append) == 0.0
    assert slept == []


def test_an_unreadable_sensor_reads_as_cool(monkeypatch, slept):
    """0 is "no reading". A governor that treated it as hot would wedge the
    index on any machine whose nvidia-smi output differs."""
    _at(monkeypatch, 0)
    assert gpu.cool_down(sleep=slept.append) == 0.0


def test_the_threshold_is_exclusive_at_its_own_value(monkeypatch, slept):
    """At exactly the threshold the card is already throttling, so the wait
    has to fire -- an off-by-one here is invisible in production."""
    _at(monkeypatch, 84, 83)
    assert gpu.cool_down(sleep=slept.append) == 5.0
