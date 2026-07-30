"""Event-driven file watcher: watchfiles (Rust `notify`) backend.

One background thread runs a single `watchfiles.watch()` generator across all
watched project roots — one inotify instance total, not one per root. Storms
are coalesced in Rust (debounce/step) before crossing into Python, and
`watch_filter` drops ignored paths using the same HR35 resolver the drift
gate uses, so a churn storm in a git-ignored/hidden dir never reaches
`on_change`. Polling fallback (NFS/SMB/WSL) is handled internally by the
Rust `notify` crate (`force_polling`) — there is no hand-rolled poll loop.

That reader thread does nothing but read. `on_change` runs on a small pool of
dispatch workers, because it reaches tree-sitter extraction, a whole-federation
BPRE scan and network calls — and while the reader sat in that stack, it was
also the only consumer of the `watchfiles` generator, so *every* project on the
box went blind for as long as one project's pass took. Work is coalesced per
project into `_pending`, so a save storm cannot grow the backlog, and a project
already in `_inflight` is never picked up twice at once (`on_change` keeps
per-project state that assumes one pass at a time). Workers block on a
`Condition` with no timeout: the only clock in this module remains the kernel's.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

log = logging.getLogger(__name__)

# An event under no registered root is dropped — correct, but it used to be silent, which is
# how a project can stop being watched without anything anywhere saying so. Rate-limited
# because a single unregistered churn storm would otherwise be the loudest thing in the journal.
_UNATTRIBUTED_LOG_INTERVAL_S = 30.0


def _worker_count() -> int:
    try:
        return max(1, int(os.environ.get("RSE_WATCHER_WORKERS", "2")))
    except ValueError:
        return 2


class Watcher:
    """Event-driven via OS filesystem notifications (watchfiles/Rust `notify`)."""

    def __init__(self, on_change: Callable[[str, list[Path]], None]) -> None:
        self._on_change = on_change
        # Replaced wholesale rather than mutated: the reader thread iterates it in `_owning_root`
        # while `sync()` may be rebinding it from a registry write, and a mutated `set` raises
        # mid-iteration. Rebinding a frozenset is atomic under the GIL, so readers see either
        # the old set or the new one and never a torn one.
        self._paths: frozenset[str] = frozenset()
        self._paths_lock = threading.Lock()
        self._last_unattributed_log = 0.0
        self._stop = threading.Event()
        self._restart = threading.Event()
        self._restart_ack = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending: dict[str, set[Path]] = {}   # project -> coalesced files awaiting a pass
        self._inflight: set[str] = set()           # projects a worker is currently passing over
        self._cv = threading.Condition()
        self._workers: list[threading.Thread] = []
        # Monotonic count of completed passes. `pending`/`inflight` are instantaneous, so they
        # answer "is it busy now" and cannot answer "did anything happen while I wasn't looking" —
        # which is the question an idle-CPU measurement has to ask about its own window.
        self._dispatched = 0

    def watch(self, project_path: str) -> None:
        self.sync(self._paths | {project_path})

    def unwatch(self, project_path: str) -> None:
        """Drop a root. Symmetric with `watch()` — without it a disabled project stays
        watched for the daemon's whole lifetime, holding kernel watches nobody reads."""
        self.sync(self._paths - {project_path})

    def sync(self, paths: Iterable[str]) -> None:
        """Arm exactly `paths`, applying **one** re-arm for the whole diff.

        Never one re-arm per path: each one tears the single `watchfiles` generator down and
        back up, and 160 roots' worth of teardown would be its own outage. Callers pass the
        desired set (the registry's enabled, non-federation-excluded projects) and this works
        out the difference.
        """
        desired = frozenset(paths)
        with self._paths_lock:
            current = self._paths
            if desired == current:
                return
            self._paths = desired
            added, removed = desired - current, current - desired
        log.debug("watcher: root set changed +%d -%d", len(added), len(removed))
        if self._thread is not None and self._thread.is_alive():
            self._restart_ack.clear()
            self._restart.set()
            # Block until the loop has torn down the old watch and is about to
            # arm the new one — otherwise a write landing in that gap is lost
            # (`notify` doesn't retroactively see events from before a watch starts).
            self._restart_ack.wait(timeout=6.0)

    def start(self) -> None:
        self._stop.clear()
        # Reader first: `notify` sees nothing that happened before the watch is armed, so every
        # instruction between here and `_watch()` is a window where a write is lost outright.
        # Workers have nothing to do until an event lands, and a worker starting late still finds
        # already-queued work — `_dispatch_worker` checks `_pending` before it ever waits.
        self._thread = threading.Thread(target=self._loop, daemon=True, name="rse-watcher")
        self._thread.start()
        self._workers = [
            threading.Thread(target=self._dispatch_worker, daemon=True, name=f"rse-sweeps-{i}")
            for i in range(_worker_count())
        ]
        for t in self._workers:
            t.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        # One shared budget, not `timeout` per thread: a worker parked in a long `on_change` must
        # not stretch shutdown, or systemd's SIGKILL lands mid-pass and orphans the parse workers.
        deadline = time.monotonic() + timeout
        for t in ([self._thread] if self._thread else []) + self._workers:
            t.join(timeout=max(0.0, deadline - time.monotonic()))

    def _owning_root(self, path: str) -> str | None:
        """Boundary-aware, longest-match root lookup (mirrors server/mcp.py::_resolve_roots).

        A raw string-prefix match would misattribute events for a root whose name is a
        string extension of a sibling root's (e.g. `foo` vs `foo-bar`) to the shorter root.
        """
        p = Path(path)
        best: str | None = None
        for proj in self._paths:
            try:
                p.relative_to(proj)
            except ValueError:
                continue
            if best is None or len(proj) > len(best):
                best = proj
        return best

    def _filter(self, _change: object, path: str) -> bool:
        from rag_search.index.discover import is_ignored_path

        root = self._owning_root(path)
        if root is None:
            now = time.monotonic()
            if now - self._last_unattributed_log >= _UNATTRIBUTED_LOG_INTERVAL_S:
                self._last_unattributed_log = now
                log.debug("watcher: unattributed %s (no registered root owns it)", path)
            return False
        return not is_ignored_path(Path(path), Path(root))

    def status(self) -> dict[str, object]:
        """What this watcher is actually watching, right now.

        Nothing logged the root set and no endpoint exposed it, so answering "is this project
        being watched?" meant a py-spy dump of the reader thread's locals. That is the whole
        reason a stale index took a day to diagnose.
        """
        with self._cv:
            pending = {root: len(files) for root, files in self._pending.items()}
            inflight = sorted(self._inflight)
        return {
            "roots": sorted(self._paths),
            "pending": pending,
            "inflight": inflight,
            "reader_alive": bool(self._thread is not None and self._thread.is_alive()),
            "workers_alive": sum(1 for t in self._workers if t.is_alive()),
            "dispatched": self._dispatched,
        }

    def _enqueue(self, root: str, files: list[Path]) -> None:
        with self._cv:
            self._pending.setdefault(root, set()).update(files)
            self._cv.notify()

    def _next_ready(self) -> str | None:
        """First project with work that no worker holds. Caller must hold `_cv`."""
        return next((r for r in self._pending if r not in self._inflight), None)

    def _dispatch_worker(self) -> None:
        while not self._stop.is_set():
            with self._cv:
                root = self._next_ready()
                while root is None and not self._stop.is_set():
                    # No timeout, deliberately: a worker wakes on an enqueue or on stop(), never
                    # on a clock. A timeout here would be a poll loop wearing a Condition's coat.
                    self._cv.wait()
                    root = self._next_ready()
                if root is None:
                    return
                files = sorted(self._pending.pop(root))
                self._inflight.add(root)
            try:
                self._on_change(root, files)
            except Exception as exc:
                log.warning("watcher %s: %s", root, exc)
            finally:
                with self._cv:
                    self._inflight.discard(root)
                    self._dispatched += 1
                    self._cv.notify()

    def _loop(self) -> None:
        from watchfiles import watch as _watch

        stop_or_restart = _StopOrRestart(self._stop, self._restart)
        while not self._stop.is_set():
            roots = sorted(self._paths)
            if not roots:
                self._stop.wait(timeout=1.0)
                continue
            self._restart.clear()
            self._restart_ack.set()
            # Logged here rather than in `sync()` because this is where the watch is actually
            # armed — a count from the request side would go on claiming roots the loop never
            # reached if arming ever failed.
            log.info("watcher: armed %d roots", len(roots))
            log.debug("watcher: roots %s", roots)
            try:
                for changes in _watch(
                    *roots, watch_filter=self._filter, stop_event=stop_or_restart, rust_timeout=5000,
                ):
                    by_root: dict[str, list[Path]] = {}
                    for _kind, path in changes:
                        root = self._owning_root(path)
                        if root is not None:
                            by_root.setdefault(root, []).append(Path(path))
                    for root, files in by_root.items():
                        self._enqueue(root, files)
            except Exception as exc:
                log.warning("watchfiles loop error: %s — retrying", exc)
                self._stop.wait(timeout=1.0)


class _StopOrRestart:
    """Adapts (stop, restart) Events to watchfiles' `is_set()` stop_event protocol."""

    def __init__(self, stop: threading.Event, restart: threading.Event) -> None:
        self._stop = stop
        self._restart = restart

    def is_set(self) -> bool:
        return self._stop.is_set() or self._restart.is_set()
