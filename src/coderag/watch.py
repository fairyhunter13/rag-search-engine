"""One inotify instance over resolved paths, debounced in Rust before Python.

**inotify does not traverse symlinks.** Watching a symlinked directory yields
nothing at all when the target changes (notify#17, inotify-tools#64), and the
failure is silent: the watcher reports itself healthy and the index goes stale.

The design sidesteps it rather than configuring around it. Federation registers
every member under its own **resolved** path, so watching the enabled set
already covers each member exactly once, deduped -- there is no symlink in the
watch list to fail to traverse. A test that touches a root directly passes with
this feature entirely broken; the one that matters writes through a symlink.

`watchfiles` coalesces storms inside Rust, so a `git checkout` across 4,000
files crosses into Python as one batch. `watch_filter` drops ignored paths using
the same `projcfg` resolver the indexer uses, so churn in an excluded directory
never reaches the queue at all.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from watchfiles import watch as _watch

from . import config, discover, index, projcfg, registry

log = logging.getLogger(__name__)

_thread: threading.Thread | None = None
_stop = threading.Event()
_rearm = threading.Event()
_lock = threading.Lock()


def _owner(path: Path, roots: list[Path]) -> Path | None:
    """Which watched project a changed path belongs to: the longest match.

    Longest rather than first, because a project nested inside another is a
    real shape here -- a federation member can live under its root's tree, and
    the first match would hand its files to the wrong store.
    """
    owning = [r for r in roots if path == r or r in path.parents]
    return max(owning, key=lambda r: len(str(r))) if owning else None


def _roots() -> list[Path]:
    return [e.path for e in registry.enabled_projects() if e.path.is_dir()]


def rearm() -> None:
    """Ask the watcher to re-read the registry. The 60 s tick calls this."""
    _rearm.set()


def _loop() -> None:
    while not _stop.is_set():
        roots = _roots()
        if not roots:
            if _stop.wait(config.SCHEDULER_TICK_S):
                return
            continue

        _rearm.clear()
        configs = {}
        for root in roots:
            entry = registry.get(root)
            configs[root] = projcfg.effective(root, tuple(entry.roots) if entry else ())

        log.info("watching %d projects", len(roots))
        for batch in _watch(
            *roots,
            stop_event=_stop,
            debounce=config.WATCH_DEBOUNCE_MS,
            rust_timeout=config.SCHEDULER_TICK_S * 1000,
            yield_on_timeout=True,
        ):
            _dispatch(batch, roots, configs)
            if _rearm.is_set() or _stop.is_set():
                break


def _dispatch(batch, roots: list[Path], configs: dict) -> None:
    """Group the batch by project and enqueue one job each.

    Per project, never per file: a `git checkout` touching 4,000 files across
    six members is six jobs, and the content-hash diff inside each one is the
    same walk it would have done anyway.
    """
    grouped: dict[Path, set[str]] = {}
    for _change, raw in batch:
        path = Path(raw)
        project = _owner(path, roots)
        if project is None:
            continue
        try:
            rel = str(path.relative_to(project))
        except ValueError:
            continue
        if not discover.indexable(rel, configs[project]):
            continue
        grouped.setdefault(project, set()).add(rel)

    for project, paths in grouped.items():
        index.submit(project, sorted(paths), reason="watch")


def start() -> threading.Thread:
    global _thread
    with _lock:
        _stop.clear()
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="watcher", daemon=True)
            _thread.start()
        return _thread


def stop(timeout: float = 5.0) -> None:
    _stop.set()
    if _thread is not None:
        _thread.join(timeout)


def watching() -> bool:
    return _thread is not None and _thread.is_alive()
