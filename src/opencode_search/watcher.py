"""File watcher: inotify → debounce → incremental index (GPU-enforced)."""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from opencode_search.config import DEBOUNCE_DELAY_MS, MIN_FLUSH_INTERVAL_S
from opencode_search.discover import IGNORED_DIRS
from opencode_search.index_config import (
    ProjectConfig,
    effective_index_config,
    load_project_config,
    matches_any_pattern,
)

log = logging.getLogger(__name__)


@dataclass
class WatcherHandle:
    root: Path
    observer: object  # watchdog Observer
    # Managed on the asyncio loop side by _dispatch/_schedule_flush.
    debounce_task: asyncio.Task | None = None
    last_flush: float = 0.0
    flush_in_progress: bool = False
    _pending_paths: set[str] = field(default_factory=set)
    _pending_deleted: set[str] = field(default_factory=set)
    # Maps resolved real directory paths → their symlink path under the project
    # root.  Built at watcher start so event paths from inotify (which always
    # resolves symlinks) can be translated back to the paths stored in the index.
    _symlink_map: dict[str, str] = field(default_factory=dict)


def _build_symlink_map(root: str) -> dict[str, str]:
    """Walk *root* (non-recursively through symlinks) and return a mapping of
    resolved real directory paths to their symlink paths under *root*.

    Only top-level symlinked directories are registered — watchdog will handle
    recursive discovery inside each real target once it is scheduled.
    Symlink targets that fall inside *root* are skipped to avoid duplicates.
    """
    symlink_map: dict[str, str] = {}
    for dirpath, dirnames, _ in os.walk(root, followlinks=False, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for dirname in dirnames:
            subdir = Path(dirpath) / dirname
            if subdir.is_symlink() and subdir.is_dir():
                real_target = str(subdir.resolve())
                # Skip targets already under the project root — they would get
                # a duplicate inotify watch from the main recursive schedule.
                if real_target != root and not real_target.startswith(root + os.sep):
                    symlink_map[real_target] = str(subdir)
    return symlink_map


class WatcherManager:
    """Manages per-project file watchers. Thread-safe via asyncio event loop."""

    def __init__(self) -> None:
        self._handles: dict[str, WatcherHandle] = {}

    def is_active(self, root: str) -> bool:
        h = self._handles.get(root)
        return h is not None and h.observer is not None

    @staticmethod
    def _should_ignore_event_path(root: Path, candidate: str | None) -> bool:
        """Return True when a watchdog event path should not enter debounce state."""
        if not candidate:
            return True
        try:
            candidate_path = Path(candidate)
        except OSError:
            return True

        if not candidate_path.is_absolute():
            candidate_path = root / candidate_path

        try:
            relative_parts = candidate_path.relative_to(root).parts
        except ValueError:
            return True

        project_cfg: ProjectConfig
        try:
            project_cfg = load_project_config(root)
        except Exception:
            project_cfg = ProjectConfig()

        # Only treat the first path component as a linked project boundary when
        # it is a top-level symlink to an external directory (mirrors discover).
        linked_cfg: ProjectConfig | None = None
        linked_name: str | None = relative_parts[0] if relative_parts else None
        if linked_name:
            try:
                top = root / linked_name
                if top.is_symlink() and top.is_dir():
                    real_target = top.resolve()
                    if str(real_target) != str(root) and not str(real_target).startswith(str(root) + os.sep):
                        linked_cfg = load_project_config(real_target)
                    else:
                        linked_name = None
                else:
                    linked_name = None
            except Exception:
                linked_name = None
                linked_cfg = None

        index_cfg = effective_index_config(project_cfg, linked_name=linked_name, linked=linked_cfg)
        match_root = root if linked_name is None else (root / linked_name)

        # Ignore only configured ignored directories inside the watched project.
        # The final path component may be an indexable dotfile such as `.env`.
        if index_cfg.use_default_ignores:
            if any(part in IGNORED_DIRS for part in relative_parts[:-1]):
                return True
        else:
            if any(part in {".opencode", ".git", ".hg", ".svn"} for part in relative_parts[:-1]):
                return True

        # Apply config include/exclude patterns early so we do not enqueue paths
        # that will never be indexed.
        if index_cfg.exclude and matches_any_pattern(candidate_path, index_cfg.exclude, match_root):
            if not (index_cfg.include and matches_any_pattern(candidate_path, index_cfg.include, match_root)):
                return True

        return False

    async def start(
        self,
        root: str | Path,
        *,
        on_change: Callable[[list[Path], list[str]], Awaitable[None]],
    ) -> bool:
        """Start watching a project root. on_change(modified_paths, deleted_paths).

        Symlinked directories inside the project root are also watched by
        registering additional inotify schedules for each resolved real target.
        Event paths from those targets are translated back to their symlink paths
        (matching the paths stored in the index) before the debounce flush.
        """
        root = str(Path(root).resolve())
        if self.is_active(root):
            log.info("watcher already active for %s", root)
            return True

        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            # Bind to the currently-running loop so events dispatch to the
            # loop that called start() — required for FastMCP and CLI watch.
            loop = asyncio.get_running_loop()
            handle = WatcherHandle(root=Path(root), observer=None)

            # Build the real→symlink translation map before the observer starts
            # so _dispatch can use it immediately for the first events.
            handle._symlink_map = _build_symlink_map(root)
            if handle._symlink_map:
                log.info(
                    "watcher: found %d symlinked directories in %s — adding extra watches",
                    len(handle._symlink_map),
                    root,
                )

            def _translate_path(p: str) -> str:
                """Map a real resolved path back to its symlink path under the project root."""
                for real_prefix, sym_prefix in handle._symlink_map.items():
                    if p == real_prefix or p.startswith(real_prefix + os.sep):
                        return sym_prefix + p[len(real_prefix):]
                return p

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
                changed = False
                if etype in ("created", "modified", "moved"):
                    p = dest if etype == "moved" and dest else src
                    if p:
                        p = _translate_path(p)
                        if not self._should_ignore_event_path(handle.root, p):
                            before = len(handle._pending_paths)
                            handle._pending_paths.add(p)
                            changed = len(handle._pending_paths) != before
                elif etype == "deleted" and src:
                    src = _translate_path(src)
                    if not self._should_ignore_event_path(handle.root, src):
                        before = len(handle._pending_deleted)
                        handle._pending_deleted.add(src)
                        changed = len(handle._pending_deleted) != before
                if changed:
                    _schedule_flush()

            def _schedule_flush():
                if handle.debounce_task and not handle.debounce_task.done():
                    if handle.flush_in_progress:
                        return
                    handle.debounce_task.cancel()
                handle.debounce_task = asyncio.create_task(_debounced_flush())

            async def _debounced_flush():
                try:
                    await asyncio.sleep(DEBOUNCE_DELAY_MS / 1000.0)
                    import time as _time

                    now = _time.monotonic()
                    if now - handle.last_flush < MIN_FLUSH_INTERVAL_S:
                        await asyncio.sleep(MIN_FLUSH_INTERVAL_S - (now - handle.last_flush))

                    # If another flush is already indexing, leave the pending
                    # sets intact and let that flush's finally block reschedule.
                    if handle.flush_in_progress:
                        return

                    # If a file appears in both pending sets, "deleted" takes
                    # precedence because the file no longer exists on disk anyway.
                    pending_paths = set(handle._pending_paths) - handle._pending_deleted
                    modified = [Path(p) for p in pending_paths if os.path.exists(p)]
                    deleted = list(handle._pending_deleted)
                    handle._pending_paths.clear()
                    handle._pending_deleted.clear()
                    handle.last_flush = _time.monotonic()

                    if not (modified or deleted):
                        return

                    handle.flush_in_progress = True
                    try:
                        await on_change(modified, deleted)
                    except Exception as e:
                        log.error("on_change callback error: %s", e)
                    finally:
                        handle.flush_in_progress = False
                        if handle._pending_paths or handle._pending_deleted:
                            _schedule_flush()
                except asyncio.CancelledError:
                    raise

            observer = Observer()
            observer.schedule(_Handler(), root, recursive=True)
            # Register extra watches for each resolved symlink target so that
            # inotify tracks changes inside symlinked directories.
            for real_target, sym_path in handle._symlink_map.items():
                if Path(real_target).is_dir():
                    observer.schedule(_Handler(), real_target, recursive=True)
                    log.debug("watcher: extra watch %s → %s", real_target, sym_path)
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
