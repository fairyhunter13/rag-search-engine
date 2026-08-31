"""Handle lifetime: what the reap closes, and what it must not close.

The reap runs on the scheduler thread and closes handles a *different* thread
opened. Nothing else in this daemon reaches across threads like that, so the
two dangerous cases each get an arm: a search still running past the idle
threshold, and a store whose file is gone.
"""

from __future__ import annotations

import sqlite3
import threading

from coderag import config, conns, runledger, server, store


def _reset() -> None:
    conns.close_all()
    with conns._caches_lock:
        conns._caches.clear()
    conns._local.__dict__.pop("cache", None)


def test_an_idle_cache_is_closed_whole(tmp_path):
    _reset()
    for name in ("a", "b"):
        project = tmp_path / name
        project.mkdir()
        store.connect(project)
    assert len(conns.cache().conns) == 2

    assert conns.reap_idle(0) == 2
    assert conns.cache().conns == {}


def test_a_fresh_cache_survives_the_reap(tmp_path):
    _reset()
    project = tmp_path / "fresh"
    project.mkdir()
    store.connect(project)

    assert conns.reap_idle(3600) == 0
    assert len(conns.cache().conns) == 1


def test_a_deleted_store_is_closed_however_recently_it_was_used(tmp_path):
    """The cache is keyed by path, so an unlinked store keeps its disk blocks."""
    _reset()
    project = tmp_path / "gone"
    project.mkdir()
    store.connect(project)
    config.index_path(project).unlink()

    assert conns.reap_idle(3600) == 1
    assert conns.cache().conns == {}


def test_a_session_is_not_reaped_under_the_thread_running_it(tmp_path):
    """A pass longer than the threshold must keep the store it is reading."""
    _reset()
    project = tmp_path / "busy"
    project.mkdir()

    inside, done, closed = threading.Event(), threading.Event(), []

    def worker() -> None:
        with conns.session():
            conn = store.connect(project)
            inside.set()
            done.wait(10)
            try:
                conn.execute("SELECT count(*) FROM files").fetchone()
            except sqlite3.ProgrammingError:
                closed.append(True)

    thread = threading.Thread(target=worker)
    thread.start()
    inside.wait(10)
    assert conns.reap_idle(0) == 0
    done.set()
    thread.join(10)

    assert closed == []
    _reset()


def test_the_reap_reaches_a_cache_the_reaping_thread_never_opened(tmp_path):
    """The whole point of the module-level registry: one thread frees another's."""
    _reset()
    project = tmp_path / "other"
    project.mkdir()
    threading.Thread(target=store.connect, args=(project,)).start()
    threading.Thread(target=lambda: None).start()

    for _ in range(100):
        with conns._caches_lock:
            caches = [c for c in conns._caches if c.conns]
        if caches:
            break
        threading.Event().wait(0.05)

    assert conns.reap_idle(0) == 1


def test_open_count_reaches_across_threads(tmp_path):
    """The scheduler thread reports the fleet's handles, not its own."""
    _reset()
    project = tmp_path / "elsewhere"
    project.mkdir()
    opened, done, failed = threading.Event(), threading.Event(), []

    def _open() -> None:
        # A thread's exception is swallowed, so a raising `connect` reached the
        # assertion below as a bare 0 and read as the count failing.
        try:
            store.connect(project)
        except Exception as exc:
            failed.append(exc)
        finally:
            opened.set()
        done.wait(10)

    worker = threading.Thread(target=_open)
    worker.start()
    try:
        assert opened.wait(10)
        assert failed == []
        assert conns.open_count() >= 1
        assert conns.cache().conns == {}
    finally:
        done.set()
        worker.join(10)


def test_a_reap_that_closes_nothing_writes_no_row(tmp_path, monkeypatch):
    """It runs every tick, so a row per tick would bury the one that matters."""
    _reset()
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)

    server._reap_stores()

    assert runledger.read(kind="reap") == []


def test_a_reap_records_what_it_closed(tmp_path, monkeypatch):
    _reset()
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "STORE_IDLE_S", 0)
    project = tmp_path / "stale"
    project.mkdir()
    store.connect(project)

    server._reap_stores()

    rows = runledger.read(kind="reap")
    assert [(r["closed"], r["open"]) for r in rows] == [(1, 0)]
