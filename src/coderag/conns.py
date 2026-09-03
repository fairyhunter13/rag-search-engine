"""Who holds a SQLite handle open, and who is allowed to close it.

A connection is not free at rest: SQLite gives each one its own page cache, and
one federated search opens the whole unit on whichever worker runs it. The cache
has to be per thread, because a connection is not shareable across threads. So
the thread that could reap a handle is never the thread that opened it, and the
list below is what makes one thread's handles visible to another at all.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config

_local = threading.local()


class _Cache:
    """One thread's open stores, and the stamp another thread reaps them by."""

    __slots__ = ("conns", "last_used", "lock")

    def __init__(self) -> None:
        self.conns: dict[str, sqlite3.Connection] = {}
        self.lock = threading.RLock()
        self.last_used = time.monotonic()


_caches: list[_Cache] = []
_caches_lock = threading.Lock()


def cache() -> _Cache:
    """This thread's cache, registered on first use and stamped on every use."""
    live = getattr(_local, "cache", None)
    if live is None:
        live = _local.cache = _Cache()
        with _caches_lock:
            _caches.append(live)
    live.last_used = time.monotonic()
    return live


@contextlib.contextmanager
def session():
    """Hold this thread's handles open for the whole of a long call.

    `reap_idle` skips a cache it cannot lock, so a pass that runs past the idle
    threshold does not have its store closed underneath it.
    """
    live = cache()
    with live.lock:
        try:
            yield
        finally:
            live.last_used = time.monotonic()


_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def pool() -> ThreadPoolExecutor:
    """One executor for the process, built once and never replaced.

    `_caches` only ever grows, so a pool built per search would register a
    `_Cache` per worker per search and leave every one of them behind. Reusing
    the threads is also what bounds the handle set: `FANOUT_WORKERS` times the
    unit, and not `THREAD_LIMIT` times it.
    """
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(
                max_workers=max(1, config.FANOUT_WORKERS), thread_name_prefix="coderag-fanout"
            )
        return _pool


def fanout(fn, items):
    """`fn` over each item, on the shared pool, results in no fixed order.

    The session is entered on the worker and never on the caller. The lock
    `reap_idle` skips is per thread, so a session held by the thread that
    dispatches guards nothing: under a pool that thread opens no store at all.
    """
    items = list(items)
    if len(items) < 2 or config.FANOUT_WORKERS < 2:
        with session():
            return [fn(item) for item in items]

    def guarded(item):
        with session():
            return fn(item)

    return list(pool().map(guarded, items))


def reap_idle(seconds: float | None = None) -> int:
    """Close whole caches gone quiet, and any handle whose store is deleted.

    The deleted case is not cosmetic. The cache is keyed by path, so an unlinked
    store keeps its handle and its disk blocks until something closes it.
    """
    seconds = config.STORE_IDLE_S if seconds is None else seconds
    now = time.monotonic()
    with _caches_lock:
        live_caches = list(_caches)
    closed = 0
    for live in live_caches:
        if not live.lock.acquire(blocking=False):
            continue
        try:
            idle = now - live.last_used >= seconds
            for key, conn in list(live.conns.items()):
                if idle or not Path(key).exists():
                    conn.close()
                    del live.conns[key]
                    closed += 1
        finally:
            live.lock.release()
    return closed


def open_count() -> int:
    """Handles live across every thread, which no single thread can count."""
    with _caches_lock:
        live_caches = list(_caches)
    return sum(len(live.conns) for live in live_caches)


def close_all() -> None:
    live = getattr(_local, "cache", None)
    if live is None:
        return
    with live.lock:
        for conn in live.conns.values():
            conn.close()
        live.conns = {}
