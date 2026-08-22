"""projects.json: one row per resolved path, mutated under an flock.

Two rules here were bought with incidents and are not negotiable.

The load happens *inside* the lock. Reading first and locking second is a
lost update: two writers each read 180 rows, each add one, and the second
write drops the first's -- measured once as a registry that kept 34 of 180.

Nothing prunes a row because its path is missing from disk. An unmounted
volume, a repo moved for ten seconds, or a member behind a broken symlink all
look identical to a deleted project, and the pruning version of this file wiped
the fleet registry when a caller ran it in-process against the real state.
Removal is always explicit.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import shutil
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from . import config
from .entry import ProjectEntry

Rows = dict[str, ProjectEntry]


def resolve(path: Path | str) -> Path:
    """The registry's key. Symlinks are a discovery mechanism, never a key."""
    return Path(path).expanduser().resolve()


def _read_unlocked() -> Rows:
    try:
        raw = json.loads(config.REGISTRY_PATH.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"registry at {config.REGISTRY_PATH} is unreadable: {exc}") from exc
    return {p: ProjectEntry.from_json(p, row) for p, row in raw.items()}


def _rotate_backup() -> None:
    if not config.REGISTRY_PATH.exists():
        return
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    shutil.copy2(config.REGISTRY_PATH, config.BACKUP_DIR / f"projects.{stamp}.json")
    keep = sorted(config.BACKUP_DIR.glob("projects.*.json"), reverse=True)[config.BACKUP_KEEP :]
    for stale in keep:
        stale.unlink(missing_ok=True)


def _write_unlocked(rows: Rows) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    _rotate_backup()
    payload = {p: rows[p].to_json() for p in sorted(rows)}
    tmp = config.REGISTRY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True))
    tmp.replace(config.REGISTRY_PATH)


@contextmanager
def _mutate() -> Generator[Rows]:
    """Exclusive registry access. Mutate the yielded dict; it is written on exit.

    Never take this while holding the GPU lock: that lock is the innermost one,
    and an index batch waiting on a registry write held by a query is a
    deadlock with no timeout on either side.
    """
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with config.REGISTRY_LOCK.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            rows = _read_unlocked()
            yield rows
            _write_unlocked(rows)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def _held(mode: int) -> Generator[Rows]:
    """The rows, with the lock still held while the caller reads them."""
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with config.REGISTRY_LOCK.open("w") as lock:
        fcntl.flock(lock, mode)
        try:
            yield _read_unlocked()
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def load() -> Rows:
    """A snapshot, shared-locked so it never observes a half-written file."""
    with _held(fcntl.LOCK_SH) as rows:
        return rows


def fleet_digest(rows: Rows) -> str:
    """A hash of what every row *is*, not how many there are.

    A count is blind to a cancelling pair, to the same count over a different
    set, to a disabled row, and to content drift -- a dead root left in a live
    project's `roots` narrows its corpus through the excludes it inherits, with
    the count unmoved. No path is disclosed: the key is hashed with the rest.
    """
    material = sorted(
        (key, e.enabled, e.direct, tuple(sorted(e.roots)), e.last_error is not None)
        for key, e in rows.items()
    )
    return hashlib.sha256(json.dumps(material).encode()).hexdigest()[:16]


def _unclaimed(rows: Rows) -> list[Path]:
    claimed = {config.index_path(e.path).parent for e in rows.values()}
    return sorted(p for p in config.INDEX_DIR.glob("*") if p.is_dir() and p not in claimed)


def _idle_for(store_dir: Path, seconds: float) -> bool:
    newest = max((f.stat().st_mtime for f in store_dir.rglob("*")), default=0.0)
    return time.time() - newest >= seconds


def unclaimed_stores() -> list[Path]:
    """Store directories no row names.

    The rows and the glob come from inside one lock. Reading rows first and
    globbing after enumerates a project claimed in between as unclaimed, and the
    caller that acts on that answer deletes a store the daemon has open.
    """
    with _held(fcntl.LOCK_SH) as rows:
        return _unclaimed(rows)


@contextmanager
def prunable_stores() -> Generator[tuple[list[Path], list[Path]]]:
    """The same walk as (prunable, busy), with the registry held exclusively.

    Nothing can be claimed for as long as this is open, so the answer cannot go
    stale between the glob and the rmtree. Busy is the gap the lock cannot
    close: an unclaimed store still being written to is a row-less job
    finishing, not garbage.
    """
    with _held(fcntl.LOCK_EX) as rows:
        found = _unclaimed(rows)
        idle = [p for p in found if _idle_for(p, config.PRUNE_MIN_IDLE_S)]
        yield idle, [p for p in found if p not in idle]


def get(path: Path | str) -> ProjectEntry | None:
    return load().get(str(resolve(path)))


def enabled_projects() -> list[ProjectEntry]:
    return [e for e in load().values() if e.enabled]


def enclosing(path: Path | str) -> Path | None:
    """The nearest enabled project containing `path`, longest match first.

    Longest rather than first, for the reason `watch._owner` gives: a member can
    live under its root's tree, and the shorter match hands the caller the wrong
    project. Enabled only -- an unflagged row has no store to answer from.
    """
    target = resolve(path)
    rows = load()
    owning = [
        e.path
        for e in rows.values()
        if e.enabled and (target == e.path or e.path in target.parents)
    ]
    return max(owning, key=lambda p: len(str(p))) if owning else None


def claim(
    path: Path | str, *, direct: bool = False, root: Path | str | None = None
) -> ProjectEntry:
    """Register a project, or record a second claim on one already registered.

    This is the whole of the late-join case: a project indexed standalone and
    later symlinked into a root gets `roots` appended, never a second row and
    never a fresh index. Whether the claim changes what gets indexed is a
    question for the config signature, not for this function.
    """
    key = str(resolve(path))
    with _mutate() as rows:
        entry = rows.get(key) or ProjectEntry(path=resolve(path))
        if direct:
            entry.direct = True
        if root is not None:
            root_key = str(resolve(root))
            if root_key != key and root_key not in entry.roots:
                entry.roots.append(root_key)
        entry.enabled = True
        rows[key] = entry
        return entry


def release(path: Path | str, *, direct: bool = False, root: Path | str | None = None) -> bool:
    """Drop one claim. Returns True when the row was removed outright.

    A member survives its root's removal if it was also claimed directly or if
    another root still names it. Nothing here touches an index directory: both
    fleet-wide index wipes in this engine's history came from code that deleted
    store directories on a computed set.
    """
    key = str(resolve(path))
    with _mutate() as rows:
        entry = rows.get(key)
        if entry is None:
            return False
        if direct:
            entry.direct = False
        if root is not None:
            root_key = str(resolve(root))
            entry.roots = [r for r in entry.roots if r != root_key]
        if entry.orphaned:
            del rows[key]
            return True
        rows[key] = entry
        return False


def set_enabled(path: Path | str, enabled: bool) -> None:
    key = str(resolve(path))
    with _mutate() as rows:
        if key in rows:
            rows[key].enabled = enabled


def update(path: Path | str, **fields) -> None:
    """Write indexing outcomes back onto a row, if it still exists.

    Silently skipping a vanished row is deliberate: the indexer finishes work
    for projects the user unflagged while it ran, and recreating the row there
    would resurrect a project nobody claims.
    """
    key = str(resolve(path))
    with _mutate() as rows:
        entry = rows.get(key)
        if entry is None:
            return
        for name, value in fields.items():
            setattr(entry, name, value)
