"""Symlinked members: discovered through the link, stored by resolved path.

The symlink is a *discovery* mechanism and nothing else. Every path this module
hands on -- to the registry, to the store, to the watcher -- is the resolved
target, because inotify does not traverse symlinks: watching the link yields
nothing when the target changes, and the failure is silent (notify#17,
inotify-tools#64). Registering members under their real paths means the watcher
covers them by construction rather than by a flag.

Dedup by resolved target is not tidiness either. On the live tree 202 symlinks
collapse to ~135 unique repos, because the same repo is linked from several
places; without the dedup every consumer does the work N times.
"""

from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import Path

from . import registry
from .projcfg import ConfigError, ProjectConfig, effective

# Depth is bounded because discovery walks a tree that contains other repos'
# working copies: an unbounded walk of a federation root enumerates every file
# in every member, which is the walk the indexer does per member anyway.
MAX_DEPTH = 4

_SKIP_DIRS = frozenset({".git", ".hg", ".svn", "node_modules", "vendor", "__pycache__", ".venv"})


def _looks_like_a_project(path: Path) -> bool:
    """A repo, or at least a directory with something in it.

    Deliberately loose: a member that is not a git repo is still a member, and
    the indexer's own filters decide what is worth reading.
    """
    if not path.is_dir():
        return False
    if (path / ".git").exists():
        return True
    try:
        return any(path.iterdir())
    except OSError:
        return False


def _excluded(rel: Path, target: Path, patterns: tuple[str, ...]) -> bool:
    """Match a federation exclude against both the link and where it points.

    Both, because the two express different intents and the live config needs
    the second: `*/_worktrees/*` describes the *target* layout -- 59 links named
    `repositories/worktrees/<svc>` resolve into a sibling `_worktrees/`
    directory, and matching only the link path re-enables every one of them.
    Those 59 carried 541,718 chunks, 24.8% of everything a federated query
    scanned, all of it a second checkout of a repo already indexed.

    fnmatch's `*` spans `/`, so a pattern matches at any depth on either side.
    """
    return any(fnmatch(str(rel), p) or fnmatch(str(target), p) for p in patterns)


def discover(root: Path | str, cfg: ProjectConfig | None = None) -> list[Path]:
    """Resolved member paths reachable by symlink from `root`, deduped.

    Returns sorted paths so that two runs over the same tree agree; the walk
    order otherwise follows inode order and would churn the registry.
    """
    root = registry.resolve(root)
    cfg = cfg or effective(root)
    seen: set[Path] = set()

    for dirpath, dirnames, _ in os.walk(root, followlinks=False):
        here = Path(dirpath)
        depth = len(here.relative_to(root).parts)
        if depth >= MAX_DEPTH:
            dirnames[:] = []
            continue

        for name in list(dirnames):
            link = here / name
            if name in _SKIP_DIRS:
                dirnames.remove(name)
                continue
            if not link.is_symlink():
                continue
            # A symlink is never descended into: its target is registered as a
            # project in its own right and walked there.
            dirnames.remove(name)

            try:
                target = link.resolve(strict=True)
            except (OSError, RuntimeError):
                continue  # broken link, or a symlink cycle
            if target == root or root in target.parents:
                continue  # points back inside the root; already covered by its walk
            if _excluded(link.relative_to(root), target, cfg.federation_exclude):
                continue
            if _looks_like_a_project(target):
                seen.add(target)

    return sorted(seen)


def register(root: Path | str) -> list[Path]:
    """Flag a root and every member it reaches. Returns the members.

    A member already registered -- indexed standalone, or claimed by another
    root -- gains a claim rather than a second row. Whether that claim changes
    what it indexes is the config signature's question, answered on its next
    pass.
    """
    root = registry.resolve(root)
    members = discover(root)
    registry.claim(root, direct=True)
    for member in members:
        registry.claim(member, root=root)
    return members


def sweep() -> list[Path]:
    """Re-discover every direct root's members. Returns what was newly claimed.

    Discovery ran only inside an explicit `index` call, so a symlink added to a
    root afterwards was never seen -- on this fleet 135 of 149 enabled rows are
    members of one root, and every one of them arrived by someone remembering
    to re-run the tool.

    It only ever adds. A link that is gone and a link whose target is briefly
    unmounted are the same observation from here, and the registry's rule is
    that removal is explicit -- the pruning version of that rule is what wiped
    the fleet once already.
    """
    claimed: list[Path] = []
    for entry in registry.enabled_projects():
        if not entry.direct or not entry.path.is_dir():
            continue
        root_key = str(entry.path)
        try:
            members = discover(entry.path)
        except ConfigError as exc:
            # The same broken file that drops it from the watch set. Recorded,
            # not raised: one unparseable repo must not stop the sweep.
            registry.record_error(entry.path, str(exc))
            continue
        for member in members:
            row = registry.get(member)
            if row is not None and root_key in row.roots:
                continue
            registry.claim(member, root=entry.path)
            claimed.append(member)
    return claimed


def unregister(root: Path | str) -> list[Path]:
    """Drop a root's claim on itself and its members. Returns the rows released.

    Released, not deleted: a member that was also claimed directly survives the
    release, and it is the one that most needs reporting -- it goes on being
    searched, under excludes nobody holds any more, until something re-walks it.
    Reporting only the deletions hid exactly that row.

    Works off the registry rather than a fresh walk: the symlinks may already
    be gone, and a member whose link was deleted is exactly the one that must
    still be released. Nothing here deletes an index directory -- both
    fleet-wide index wipes came from code that did.
    """
    root = registry.resolve(root)
    root_key = str(root)
    removed: list[Path] = []

    for key, entry in registry.load().items():
        if root_key in entry.roots:
            registry.release(key, root=root)
            removed.append(Path(key))
    if registry.release(root, direct=True):
        removed.append(root)
    return removed


def members_of(root: Path | str) -> list[Path]:
    """The root's members according to the registry, not according to disk.

    Search reads this: a link deleted five seconds ago must not silently drop a
    member out of the corpus mid-query. Discovery reconciles that on its own
    schedule.
    """
    root_key = str(registry.resolve(root))
    return sorted(Path(k) for k, e in registry.load().items() if root_key in e.roots and e.enabled)


def expand(root: Path | str) -> list[Path]:
    """The search unit: the root together with its federated members."""
    root = registry.resolve(root)
    return [root, *members_of(root)]
