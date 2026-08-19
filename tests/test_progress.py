"""No GPU, no daemon, no store: the tracker is arithmetic and one atomic write.

Every assertion here is written to fail if the behaviour it names is removed.
The throttle tests are the ones that matter -- a tracker that writes on every
call still passes a "the file has the right number in it" test, and so does one
that never throttles at all.
"""

from __future__ import annotations

import json

import pytest

from coderag import config, progress


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Module globals, so state leaks between tests unless it is reset."""
    monkeypatch.setattr(config, "PROGRESS_PATH", tmp_path / "progress.json")
    monkeypatch.setattr(config, "PROGRESS_WRITE_S", 0)
    monkeypatch.setattr(progress, "_state", progress.Progress())
    monkeypatch.setattr(progress, "_last_write", 0.0)
    return tmp_path


def test_begin_publishes_before_any_file_is_done():
    """A run that is 0% done is exactly when someone asks what is happening."""
    progress.begin("/repo", total=100)
    on_disk = progress.read()
    assert on_disk["project"] == "/repo"
    assert on_disk["files_total"] == 100
    assert on_disk["files_done"] == 0
    assert on_disk["phase"] == "indexing"


def test_the_throttle_actually_withholds_a_write(monkeypatch):
    """Discriminating: without the throttle the on-disk count would keep up."""
    monkeypatch.setattr(config, "PROGRESS_WRITE_S", 3600)
    progress.begin("/repo", total=10)
    progress.advance()
    progress.advance()

    assert progress._state.done == 2, "in-process state must always be current"
    assert progress.read()["files_done"] == 0, (
        "the throttle let a write through; at one write per file this is an "
        "fsync per file on the path that already commits every 64"
    )


def test_finish_beats_the_throttle(monkeypatch):
    """The loss-window bug, as a test.

    `eval.py` printed only at the end and five arms went to a 0-byte file. The
    same shape here would be a run that completes and never says so.
    """
    monkeypatch.setattr(config, "PROGRESS_WRITE_S", 3600)
    progress.begin("/repo", total=2)
    progress.advance()
    progress.advance()
    progress.finish()

    on_disk = progress.read()
    assert on_disk["files_done"] == 2
    assert on_disk["phase"] == "idle"


def test_a_cooling_pause_is_named_not_merely_slow(monkeypatch):
    monkeypatch.setattr(config, "PROGRESS_WRITE_S", 3600)
    progress.begin("/repo", total=10)
    progress.phase("cooling")
    assert progress.read()["phase"] == "cooling", (
        "a paused card is indistinguishable from a wedged one without this"
    )
    progress.cooled(12.0)
    assert progress.read()["cooled_s"] == 12.0


def test_eta_is_wall_clock_so_pauses_are_priced_in():
    """Half done in 100 s of which 40 s was cooling: 100 s left, not 60 s.

    An ETA off working time alone assumes the pauses stop, which they do not.
    """
    state = progress.Progress(
        project="/repo", done=5, total=10, cooled_s=40.0, started_at=0.0, updated_at=100.0
    )
    snap = progress.snapshot(state)
    assert snap["eta_s"] == 100.0
    assert snap["percent"] == 50.0
    assert snap["elapsed_s"] == 100.0
    # Throughput is the other question -- what the card does when it is working.
    assert snap["files_per_s"] == round(5 / 60.0, 3)


def test_a_total_of_zero_reports_no_percent_rather_than_dividing():
    state = progress.Progress(project="/repo", done=0, total=0, started_at=0.0, updated_at=5.0)
    snap = progress.snapshot(state)
    assert "percent" not in snap
    assert "eta_s" not in snap
    assert snap["phase"] == "idle"


def test_snapshot_is_empty_before_anything_begins():
    assert progress.snapshot() == {}


@pytest.mark.parametrize("body", ["", "{ truncated", "null"])
def test_a_reader_survives_a_half_written_file(isolate, body):
    """The writer renames into place, but a reader still meets an empty file
    the first time it looks, and must not raise into whatever it was doing."""
    isolate.joinpath("progress.json").write_text(body, encoding="utf-8")
    assert progress.read() in ({}, None) or isinstance(progress.read(), dict)


def test_read_of_a_missing_file_is_empty():
    assert progress.read() == {}


def test_the_write_is_atomic_leaving_no_partial_file(isolate):
    progress.begin("/repo", total=3)
    progress.advance()
    progress.finish()
    assert json.loads(isolate.joinpath("progress.json").read_text())["files_done"] == 1
    assert list(isolate.glob("*.tmp")) == [], "a temp file survived the rename"
