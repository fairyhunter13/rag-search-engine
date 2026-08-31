"""Removing a project the filesystem says is gone, on the event and never a scan.

`registry.py` refused this until 2026-08-30, and the refusal was bought with an
incident: a prune by predicate emptied a fleet when a volume went away. Its
argument was that an unmounted volume, a repo moved for ten seconds and a member
behind a broken link all look the same. That is true of a scan. It is not true of
a delete event, and this module is the difference.

Three tests separate a deletion from the other two, and a row dies only when all
three agree.

1. The trigger is one `deleted` event on the path itself. A sweep of the disk
   never starts a removal here.
2. The parent directory must still exist. A repo removed leaves its parent
   standing. An unmounted volume takes the parent with it.
3. A grace period must pass, and the path must still be gone at the end of it.
   A `git clone` into a moved-aside path settles well inside it.

The link case is separate and weaker. A member link removed while its target
lives releases one root's claim, and `registry.release` already deletes the row
only when nothing else claims it.

The structural engine carries the same rule in its own `prune.py`. A removal
rule in one engine only makes the two member sets diverge the first time a
repository is removed.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import config, federation, projcfg, quarantine, registry

log = logging.getLogger(__name__)

# Long enough for a checkout, a restore or a move to settle. Short enough that
# the two engines do not disagree about the fleet for a whole minute.
GRACE_SECONDS = 30.0


def looks_deleted(path: Path | str) -> bool:
    """The path is gone and its parent is not. The unmount test, in one line."""
    target = Path(path)
    if target.exists() or target.is_symlink():
        return False
    parent = target.parent
    return parent != target and parent.is_dir()


def _is_idle(store: Path) -> bool:
    """Nothing has written the store for `PRUNE_MIN_IDLE_S`.

    The defect this holds is recorded: a prune raced a store the daemon was
    mid-write on. `prunable_stores` applies the same floor to the orphan walk;
    the event path never went through it, so it gets the floor here.
    """
    try:
        age = time.time() - store.stat().st_mtime
    except OSError:
        return False
    return age >= config.PRUNE_MIN_IDLE_S


def _retire_stores(keys: list[str]) -> list[str]:
    """Move each dead row's store into quarantine. Returns the ones that moved.

    A row leaving used to free no bytes at all: the watcher removed the row and
    the store stayed on disk until a human typed `doctor --prune`. This is that
    half, and it moves rather than deletes -- see `quarantine`.
    """
    moved: list[str] = []
    for key in keys:
        store = config.index_path(key)
        if not _is_idle(store):
            log.info("leaving %s: written inside the idle floor", store.parent)
            continue
        if quarantine.take(store.parent) is not None:
            moved.append(key)
    return moved


def verdict(entry) -> str:
    """`present`, `deleted`, `unmounted` or `unknown`, for a row on a cold start.

    inotify has no replay, so a repo removed while the daemon was down is
    invisible to the event path forever. This is the one reconciliation that
    looks at state rather than an event, which is exactly what `registry.py`
    refuses to do by predicate -- so it answers four ways instead of two, and
    only `deleted` is actionable.

    `looks_deleted` alone is not enough here. A mount point left standing after
    its volume goes away is an empty directory whose parent exists, and that is
    the shape a deleted repo has. The recorded `st_dev` is what separates them:
    a different filesystem answering the path today means the volume moved, not
    that the repo went. A row with no recorded device answers `unknown`.
    """
    path = Path(entry.path)
    if path.exists() or path.is_symlink():
        return "present"
    ancestor = next((p for p in path.parents if p.exists()), None)
    if ancestor is None:
        return "unmounted"
    if not entry.dev:
        return "unknown"
    try:
        if ancestor.stat().st_dev != entry.dev:
            return "unmounted"
    except OSError:
        return "unknown"
    return "deleted"


def survey() -> dict[str, list[str]]:
    """Every row, by verdict. Reports and never acts: the read-only half."""
    out: dict[str, list[str]] = {"deleted": [], "unmounted": [], "unknown": []}
    for key, entry in registry.load().items():
        answer = verdict(entry)
        if answer != "present":
            out[answer].append(key)
    return {k: sorted(v) for k, v in out.items()}


@dataclass(slots=True)
class _Pending:
    due: float


class Pruner:
    """Deletions waiting out their grace period. One instance per process.

    The clock is injected so a test spends no wall time. Nothing here starts a
    thread: `run_due` is called by the watcher loop, which is already awake.
    """

    def __init__(self, *, grace: float = GRACE_SECONDS, clock=time.monotonic) -> None:
        self._grace = grace
        self._clock = clock
        self._lock = threading.Lock()
        self._gone: dict[str, _Pending] = {}
        self._unlinked: dict[str, _Pending] = {}

    def note_gone(self, key: Path | str) -> None:
        """A registered project's own directory was deleted."""
        with self._lock:
            self._gone[str(Path(key))] = _Pending(due=self._clock() + self._grace)

    def note_unlinked(self, root: Path | str) -> None:
        """A member link under `root` was deleted. The target may well survive."""
        with self._lock:
            self._unlinked[str(Path(root))] = _Pending(due=self._clock() + self._grace)

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._gone) + len(self._unlinked)

    def _take_due(self) -> tuple[list[str], list[str]]:
        now = self._clock()
        with self._lock:
            gone = [k for k, p in self._gone.items() if p.due <= now]
            unlinked = [k for k, p in self._unlinked.items() if p.due <= now]
            for key in gone:
                del self._gone[key]
            for key in unlinked:
                del self._unlinked[key]
        return sorted(gone), sorted(unlinked)

    def run_due(self) -> dict[str, list[str]]:
        """Act on every deletion whose grace period has run out.

        Returns what it did, so the caller writes one ledger line rather than
        reading the registry back to find out.
        """
        gone, unlinked = self._take_due()
        if not gone and not unlinked:
            # The loop calls this on every tick, including the empty one that
            # `yield_on_timeout` produces. Nothing due must cost nothing.
            return {"forgotten": [], "unclaimed": [], "quarantined": []}
        rows = registry.load()
        # Re-confirmed here, at the end of the grace period. A path that came
        # back inside the window reaches this line and fails the test.
        dead = [key for key in gone if key in rows and looks_deleted(key)]
        forgotten: list[str] = []
        quarantined: list[str] = []
        if dead:
            dropped, released = registry.forget(dead)
            forgotten = sorted({*dropped, *released})
            log.info("forgot %d deleted project(s): %s", len(forgotten), ", ".join(forgotten))
            quarantined = _retire_stores(forgotten)

        # `unclaimed` is the claim dropped, and `forgotten` is the row gone.
        # They differ whenever a second root still claims the member, which is
        # the whole reason this path calls `release` and never `forget`.
        unclaimed: list[str] = []
        for root in unlinked:
            if not Path(root).is_dir():
                continue
            for member in federation.release_gone(root):
                unclaimed.append(str(member))
                if registry.get(member) is None:
                    forgotten.append(str(member))
        if unclaimed:
            log.info("released %d member(s) whose link is gone", len(unclaimed))
        quarantine.expire()
        return {
            "forgotten": sorted(set(forgotten)),
            "unclaimed": sorted(set(unclaimed)),
            "quarantined": sorted(set(quarantined)),
        }


PRUNER = Pruner()


# Every registry key, and every member link mapped to the root that reaches it.
# A delete event on either is the only thing that starts a removal, and both are
# rebuilt whenever the watch set is re-armed.
_keys: frozenset[str] = frozenset()
_links: dict[str, str] = {}


def register_paths(roots: list[Path]) -> None:
    """The two lookup tables a delete event is matched against.

    Only a directly enrolled project is walked for links. A member is a leaf
    that some root reached, and walking all of them would cost hundreds of tree
    walks before the first watch is armed. inotify has no replay, so time spent
    here is time the fleet is blind.
    """
    global _keys, _links
    rows = registry.load()
    _keys = frozenset(rows)
    watched = {str(root) for root in roots}
    found = {}
    for key, row in rows.items():
        if not row.direct or key not in watched:
            continue
        try:
            for link in federation.links(key):
                found[str(link)] = key
        except projcfg.ConfigError:
            continue
    _links = found


def note_deletions(batch) -> int:
    """Queue what the filesystem says is gone. Nothing is removed here.

    The removal itself waits out a grace period in `prune.py`, and it is
    re-confirmed there. A path that comes back inside the window keeps its row.
    """
    noted = 0
    for _change, raw in batch:
        if raw in _keys and looks_deleted(raw):
            PRUNER.note_gone(raw)
            noted += 1
            continue
        owner = _links.get(raw)
        if owner and not Path(raw).is_symlink():
            PRUNER.note_unlinked(owner)
            noted += 1
    return noted
