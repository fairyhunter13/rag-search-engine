"""Event-driven file watcher: watchfiles (Rust `notify`) backend.

One background thread runs a single `watchfiles.watch()` generator across all
watched project roots — one inotify instance total, not one per root. Storms
are coalesced in Rust (debounce/step) before crossing into Python, and
`watch_filter` drops ignored paths using the same HR35 resolver the drift
gate uses, so a churn storm in a git-ignored/hidden dir never reaches
`on_change`. Polling fallback (NFS/SMB/WSL) is handled internally by the
Rust `notify` crate (`force_polling`) — there is no hand-rolled poll loop.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)


class Watcher:
    """Event-driven via OS filesystem notifications (watchfiles/Rust `notify`)."""

    def __init__(self, on_change: Callable[[str, list[Path]], None]) -> None:
        self._on_change = on_change
        self._paths: set[str] = set()
        self._stop = threading.Event()
        self._restart = threading.Event()
        self._restart_ack = threading.Event()
        self._thread: threading.Thread | None = None

    def watch(self, project_path: str) -> None:
        if project_path in self._paths:
            return
        self._paths.add(project_path)
        if self._thread is not None and self._thread.is_alive():
            self._restart_ack.clear()
            self._restart.set()
            # Block until the loop has torn down the old watch and is about to
            # arm the new one — otherwise a write landing in that gap is lost
            # (`notify` doesn't retroactively see events from before a watch starts).
            self._restart_ack.wait(timeout=6.0)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ocs-watcher")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _owning_root(self, path: str) -> str | None:
        for proj in self._paths:
            if path.startswith(proj):
                return proj
        return None

    def _filter(self, _change: object, path: str) -> bool:
        from rag_search.index.discover import is_ignored_path

        root = self._owning_root(path)
        if root is None:
            return False
        return not is_ignored_path(Path(path), Path(root))

    def _loop(self) -> None:
        from watchfiles import watch as _watch

        stop_or_restart = _StopOrRestart(self._stop, self._restart)
        while not self._stop.is_set():
            roots = list(self._paths)
            if not roots:
                self._stop.wait(timeout=1.0)
                continue
            self._restart.clear()
            self._restart_ack.set()
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
                        try:
                            self._on_change(root, files)
                        except Exception as exc:
                            log.warning("watcher %s: %s", root, exc)
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
