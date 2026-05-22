"""File watcher: inotify → debounce → incremental index (GPU-enforced)."""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Union

from opencode_search.config import DEBOUNCE_DELAY_MS, MIN_FLUSH_INTERVAL_S

log = logging.getLogger(__name__)


@dataclass
class WatcherHandle:
    root: Path
    observer: object  # watchdog Observer
    # Stored as a concurrent.futures.Future because asyncio.run_coroutine_threadsafe
    # returns one (it's called from the watchdog Observer thread).
    debounce_task: Union[concurrent.futures.Future, None] = None
    last_flush: float = 0.0
    _pending_paths: set[str] = field(default_factory=set)
    _pending_deleted: set[str] = field(default_factory=set)


class WatcherManager:
    """Manages per-project file watchers. Thread-safe via asyncio event loop."""

    def __init__(self) -> None:
        self._handles: dict[str, WatcherHandle] = {}

    def is_active(self, root: str) -> bool:
        h = self._handles.get(root)
        return h is not None and h.observer is not None

    async def start(
        self,
        root: str | Path,
        *,
        on_change: Callable[[list[Path], list[str]], Awaitable[None]],
    ) -> bool:
        """Start watching a project root. on_change(modified_paths, deleted_paths)."""
        root = str(Path(root).resolve())
        if self.is_active(root):
            log.info("watcher already active for %s", root)
            return True

        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            # Bind to the currently-running loop so events dispatch to the
            # loop that called start() — required for FastMCP and CLI watch.
            loop = asyncio.get_running_loop()
            handle = WatcherHandle(root=Path(root), observer=None)

            class _Handler(FileSystemEventHandler):
                def on_any_event(self, event):
                    if event.is_directory:
                        return
                    src = getattr(event, "src_path", None)
                    dest = getattr(event, "dest_path", None)
                    etype = event.event_type  # created/modified/deleted/moved
                    asyncio.run_coroutine_threadsafe(
                        _dispatch(src, dest, etype), loop
                    )

            async def _dispatch(src: str | None, dest: str | None, etype: str) -> None:
                if etype in ("created", "modified", "moved"):
                    p = dest if etype == "moved" and dest else src
                    if p:
                        handle._pending_paths.add(p)
                elif etype == "deleted" and src:
                    handle._pending_deleted.add(src)
                _schedule_flush()

            def _schedule_flush():
                if handle.debounce_task and not handle.debounce_task.done():
                    handle.debounce_task.cancel()
                handle.debounce_task = asyncio.run_coroutine_threadsafe(
                    _debounced_flush(), loop
                )

            async def _debounced_flush():
                await asyncio.sleep(DEBOUNCE_DELAY_MS / 1000.0)
                import time as _time
                now = _time.monotonic()
                if now - handle.last_flush < MIN_FLUSH_INTERVAL_S:
                    await asyncio.sleep(MIN_FLUSH_INTERVAL_S - (now - handle.last_flush))
                # If a file appears in both pending sets, "deleted" takes
                # precedence because the file no longer exists on disk anyway.
                pending_paths = set(handle._pending_paths) - handle._pending_deleted
                modified = [Path(p) for p in pending_paths if os.path.exists(p)]
                deleted = list(handle._pending_deleted)
                handle._pending_paths.clear()
                handle._pending_deleted.clear()
                handle.last_flush = _time.monotonic()
                if modified or deleted:
                    try:
                        await on_change(modified, deleted)
                    except Exception as e:
                        log.error("on_change callback error: %s", e)

            observer = Observer()
            observer.schedule(_Handler(), root, recursive=True)
            observer.start()
            handle.observer = observer
            self._handles[root] = handle
            log.info("watcher started for %s", root)
            return True
        except Exception as e:
            log.error("watcher start failed for %s: %s", root, e)
            return False

    async def stop(self, root: str | Path) -> None:
        root = str(Path(root).resolve())
        handle = self._handles.pop(root, None)
        if handle and handle.observer:
            try:
                handle.observer.stop()
                await asyncio.to_thread(handle.observer.join)
            except Exception as e:
                log.warning("watcher stop error for %s: %s", root, e)
            log.info("watcher stopped for %s", root)

    async def stop_all(self) -> None:
        roots = list(self._handles.keys())
        for root in roots:
            await self.stop(root)

    def list_active(self) -> list[str]:
        return [r for r, h in self._handles.items() if h.observer is not None]


# Global singleton used by MCP server
watcher_manager = WatcherManager()
