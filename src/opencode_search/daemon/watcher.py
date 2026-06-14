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
        self._observer: object | None = None
        self._handler: object | None = None

    def watch(self, project_path: str) -> None:
        if project_path not in self._paths:
            self._paths[project_path] = self._snapshot(project_path)
        if self._observer is not None:
            import contextlib
            with contextlib.suppress(Exception):
                self._observer.schedule(self._handler, project_path, recursive=True)  # type: ignore[attr-defined]

    def unwatch(self, project_path: str) -> None:
        self._paths.pop(project_path, None)

    def start(self) -> None:
        self._stop.clear()
        if not self._try_inotify():
            self._thread = threading.Thread(target=self._loop, daemon=True, name="ocs-watcher")
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        if self._observer is not None:
            try:
                self._observer.stop()  # type: ignore[attr-defined]
                self._observer.join(timeout=timeout)  # type: ignore[attr-defined]
            except Exception:
                pass

    def _try_inotify(self) -> bool:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer as _Observer
            watcher = self
            class _H(FileSystemEventHandler):
                def on_any_event(self, event) -> None:
                    if event.is_directory:
                        return
                    src = str(getattr(event, "src_path", ""))
                    for proj in list(watcher._paths):
                        if src.startswith(proj):
                            try:
                                watcher._on_change(proj, [Path(src)])
                            except Exception as exc:
                                log.warning("inotify %s: %s", proj, exc)
                            break
            obs = _Observer()
            h = _H()
            for path in self._paths:
                obs.schedule(h, path, recursive=True)
            obs.start()
            self._observer, self._handler = obs, h
            return True
        except Exception as exc:
            log.info("inotify unavailable (%s), using poll", exc)
            return False

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
