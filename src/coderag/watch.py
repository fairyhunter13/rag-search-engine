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

import contextlib
import logging
import threading
from pathlib import Path

from watchfiles import watch as _watch

from . import config, discover, index, projcfg, registry

log = logging.getLogger(__name__)

_thread: threading.Thread | None = None
_stop = threading.Event()
_rearm = threading.Event()
_armed: tuple[Path, ...] = ()
# What this pass is arming, which is what "has the set changed" has to compare
# against; `_armed` is empty until the watches are actually in place.
_intent: tuple[Path, ...] = ()
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


def rearm_if_changed() -> None:
    """Re-arm only when the watch set actually differs. The only entry point.

    Re-arming tears down every inotify watch and rebuilds it, and on this fleet
    that is ~120,000 of them -- 5.4 s measured over 151 projects, plus up to
    `WATCH_POLL_MS` before the loop even notices the flag. inotify has no
    replay, so everything inside that window is lost for good.

    An unconditional `rearm` existed beside this and the `index` tool called it
    on **every** call, registry change or not. A caller polling `index` -- which
    is what every live test and every agent checking on a build does -- held the
    watcher blind more or less continuously, which is how a deleted file
    survived a 300 s poll three runs running while the watcher reported itself
    healthy and the delivery path tested correct at every layer.
    """
    if tuple(_roots()) != _intent:
        _rearm.set()


def _loop() -> None:
    while not _stop.is_set():
        roots = _roots()
        if not roots:
            if _stop.wait(config.SCHEDULER_TICK_S):
                return
            continue

        _rearm.clear()
        global _armed, _intent
        # The unfiltered list on purpose: a project dropped below for a broken
        # config is still enabled, so comparing against the filtered one would
        # differ on every tick and re-arm forever -- the bug this replaces.
        _intent = tuple(roots)
        _armed = ()
        configs = {}
        for root in list(roots):
            entry = registry.get(root)
            try:
                configs[root] = projcfg.effective(root, tuple(entry.roots) if entry else ())
            except projcfg.ConfigError as exc:
                # One typo in one repo's `.coderag.toml` used to take the whole
                # thread down, and a dead watcher is indistinguishable from a
                # quiet one: nothing restarts it and every other project stops
                # noticing writes. Drop the project, keep the fleet.
                # Until the registry changes or the daemon restarts: fixing the
                # file alters no row, so `rearm_if_changed` never fires on it.
                log.warning("not watching %s until it is re-registered: %s", root, exc)
                with contextlib.suppress(Exception):
                    registry.record_error(root, str(exc))

        roots = [r for r in roots if r in configs]
        watched = tuple(roots)
        if not roots:
            if _stop.wait(config.SCHEDULER_TICK_S):
                return
            continue
        log.info("watching %d projects", len(roots))
        for batch in _watch(
            *roots,
            stop_event=_stop,
            debounce=config.WATCH_DEBOUNCE_MS,
            rust_timeout=config.WATCH_POLL_MS,
            yield_on_timeout=True,
        ):
            # Only now. Establishing ~120,000 watches takes seconds, and a write
            # in that window is lost -- so anything waiting on `armed` has to
            # wait for the first yield, not for the decision to rebuild.
            # What was armed, not what was intended: the two differ by whatever
            # the config loop dropped, and `armed` is asked before a write.
            _armed = watched
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
        # inotify identifies a directory by inode, so a root and the member it
        # reaches through a symlink register the same watch, and notify reports
        # events under whichever was added last. Unresolved, a member's writes
        # arrive addressed to its root as `link/src/x.py` and the member's own
        # store is never corrected -- which is how a deleted file stayed
        # searchable indefinitely.
        path = Path(raw).resolve()
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
        if configs[project].respect_gitignore:
            paths -= discover.git_ignored(project, sorted(paths))
        if paths:
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


def armed(project: Path | str) -> bool:
    """Whether this project's watches are actually in place.

    A live thread says nothing about one project: registration sets a flag and
    the rebuild lands up to `WATCH_POLL_MS` plus ~5 s later, and a write inside
    that window is gone -- inotify replays nothing. So the honest answer to
    "is this being watched" is membership in the armed set, not thread liveness.
    """
    return registry.resolve(project) in _armed
