"""The per-project quiet window: what it merges, and what it must not delay."""

from __future__ import annotations

import time

import pytest

from coderag import index, quiet, tools


@pytest.fixture(autouse=True)
def _empty():
    quiet.flush()
    while not index._queue.empty():
        index._queue.get_nowait()
    yield
    quiet.flush()


def _queued() -> list[index.Job]:
    return [job for job in list(index._queue.queue) if job is not None]


def test_a_held_job_is_not_queued_yet(tmp_path):
    index.submit(tmp_path, ["a.py"], reason="watch", delay=60)

    assert _queued() == []
    assert quiet.pending() == 1


def test_a_second_event_widens_the_job_and_restarts_the_countdown(tmp_path):
    index.submit(tmp_path, ["a.py"], reason="watch", delay=0.05)
    index.submit(tmp_path, ["b.py"], reason="watch", delay=60)

    assert quiet.due() == []
    assert quiet.pending() == 1

    project, paths, reason = quiet.release(tmp_path)
    assert paths == ["a.py", "b.py"]
    assert (project, reason) == (tmp_path.resolve(), "watch")


def test_a_whole_project_hold_absorbs_the_partials_under_it(tmp_path):
    index.submit(tmp_path, ["a.py"], reason="watch", delay=60)
    index.submit(tmp_path, None, reason="watch", delay=60)

    assert quiet.release(tmp_path)[1] is None


def test_the_countdown_expires(tmp_path):
    index.submit(tmp_path, ["a.py"], reason="watch", delay=0)
    quiet.hold(tmp_path.resolve(), ["a.py"], "watch", -1)

    assert [p for p, _, _ in quiet.due()] == [tmp_path.resolve()]
    assert quiet.pending() == 0


def test_an_explicit_call_does_not_wait_and_takes_the_held_paths(tmp_path):
    """Otherwise the explicit pass runs narrower than the one it displaced."""
    index.submit(tmp_path, ["held.py"], reason="watch", delay=60)
    index.submit(tmp_path, ["asked.py"], reason="manual")

    assert quiet.pending() == 0
    assert [job.paths for job in _queued()] == [["asked.py", "held.py"]]


def test_an_explicit_whole_project_call_is_queued_whole(tmp_path):
    index.submit(tmp_path, ["held.py"], reason="watch", delay=60)
    index.submit(tmp_path, None, reason="manual")

    assert quiet.pending() == 0
    assert [job.paths for job in _queued()] == [None]


def test_a_zero_delay_submit_is_queued_immediately(tmp_path):
    """`WATCH_QUIET_MS=0` has to restore the per-batch pass."""
    index.submit(tmp_path, ["a.py"], reason="watch", delay=0)

    assert quiet.pending() == 0
    assert len(_queued()) == 1


def test_two_projects_hold_separately(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    index.submit(one, ["a.py"], reason="watch", delay=60)
    index.submit(two, ["b.py"], reason="watch", delay=60)

    assert quiet.pending() == 2
    assert quiet.release(one)[1] == ["a.py"]
    assert quiet.pending() == 1


def test_the_held_count_reaches_the_status_reply(tmp_path):
    index.submit(tmp_path, ["a.py"], reason="watch", delay=60)

    assert index.status()["held"] == 1


def test_a_held_project_counts_as_pending_for_its_unit(tmp_path):
    """Otherwise `index` answers 0 to "I saved a file and nothing happened"."""
    index.submit(tmp_path, ["a.py"], reason="watch", delay=60)

    assert tools._pending({tmp_path.resolve()}) == 1
    assert tools._pending({tmp_path.resolve() / "elsewhere"}) == 0


def test_the_worker_promotes_a_job_once_its_countdown_expires(tmp_path):
    """The wiring, end to end, on a path that raises before any model loads."""
    index.submit(tmp_path / "not-a-directory", None, reason="watch", delay=0.2)
    assert quiet.pending() == 1

    before = index.status()["failed"]
    index.start_worker()
    try:
        deadline = time.monotonic() + 10
        while index.status()["failed"] == before and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        index.stop_worker()

    assert index.status()["failed"] == before + 1
    assert quiet.pending() == 0
