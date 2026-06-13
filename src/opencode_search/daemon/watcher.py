"""Poll-based file watcher: detect changes in project dirs → trigger reindex."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)


class Watcher:
    """Polls registered project directories every POLL_INTERVAL seconds."""

    POLL_INTERVAL: float = 5.0

    def __init__(self, on_change: Callable[[str, list[Path]], None]) -> None:
        self._on_change = on_change
        self._paths: dict[str, dict[Path, float]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def watch(self, project_path: str) -> None:
        if project_path not in self._paths:
            self._paths[project_path] = self._snapshot(project_path)

    def unwatch(self, project_path: str) -> None:
        self._paths.pop(project_path, None)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ocs-watcher")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _snapshot(self, project_path: str) -> dict[Path, float]:
        import contextlib

        from opencode_search.index.discover import iter_files
        snap: dict[Path, float] = {}
        try:
            for f in iter_files(Path(project_path)):
                with contextlib.suppress(OSError):
                    snap[f] = f.stat().st_mtime
        except Exception:
            pass
        return snap

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(timeout=self.POLL_INTERVAL)
            if self._stop.is_set():
                break
            for project_path in list(self._paths):
                old = self._paths[project_path]
                new = self._snapshot(project_path)
                changed = [f for f in new if new.get(f) != old.get(f)]
                if changed:
                    self._paths[project_path] = new
                    try:
                        self._on_change(project_path, changed)
                    except Exception as exc:
                        log.warning("watcher callback %s: %s", project_path, exc)
