#!/usr/bin/env python3
"""One-off purge of indexed content that discovery would no longer index.

Not a missing capability: reconcile's fourth trigger is a level-triggered completeness check
for precisely this drift ("a size cap, an exclusion, a newly supported language"), purging the
same paths via the same `delete_by_path`. What it lacks is a bound — it reaches a project when
the walk gets there, and `_PAUSED` abandons that walk mid-pass, which every live test here
triggers. This makes one explicit pass and reports per store what left.

Never re-embeds, and never purges a path that is merely *gone* from disk — `_should_drop`
reports an unreadable path as keep-it so a deleted file survives `is_ignored_path` and gets
purged by the incremental indexer instead. Those are counted and reported, never touched.

Exit: 0 = clean, 1 = a store could not be purged, or the daemon would not pause.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from rag_search.core.config import index_dir
from rag_search.core.index_config import ProjectConfig, effective_config
from rag_search.core.registry import list_projects
from rag_search.daemon.federation import searchable_stores
from rag_search.index.discover import gitignore_chain_for_dir, is_ignored_path
from rag_search.index.store import VectorStore

DAEMON = "http://127.0.0.1:8765"


def _store_paths_with_mass(db: Path) -> tuple[dict[str, int], set[str]]:
    """(chunks per path, paths holding a hash row), on a separate `mode=ro` handle so a dry
    run never opens a store for writing or pays VectorStore's 128 MB write-path cache."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        mass = dict(con.execute("SELECT path, COUNT(*) FROM chunks GROUP BY path"))
        return mass, {p for (p,) in con.execute("SELECT path FROM file_hashes")}
    finally:
        con.close()


def plan_store(project: str, paths: set[str], cfg: ProjectConfig) -> tuple[list[str], int, int]:
    """(paths to purge, paths gone from disk, paths outside this project's root). Grouped by
    parent to hoist the gitignore chain the way the engine's own tree walk does —
    `_gitignore_chain_for` rebuilds the identical tuple once per file otherwise."""
    root = Path(project).resolve()
    doomed: list[str] = []
    missing = foreign = 0
    by_dir: dict[Path, list[str]] = collections.defaultdict(list)
    for p in paths:
        by_dir[Path(p).parent].append(p)
    for parent, group in by_dir.items():
        if not parent.is_relative_to(root):
            foreign += len(group)
            continue
        chain = gitignore_chain_for_dir(parent, root)
        for p in group:
            if not Path(p).exists():
                missing += 1
            elif is_ignored_path(Path(p), root, cfg, is_dir=False, chain=chain):
                doomed.append(p)
    return doomed, missing, foreign


@contextlib.contextmanager
def sweeps_paused(enabled: bool):
    """Hold the daemon's sweeps paused, restoring the state it was actually in. Not about the
    GPU — this never embeds; it drops the number of writers competing for a store's write lock.
    It does not drop it to one: the pause is checked between walk positions, so a purge already
    in flight keeps going, and the watcher's `on_change` lane is never paused at all. Hence a
    locked store is reported and skipped, not retried — a re-run finishes it."""
    previously = None
    if enabled:
        try:
            with urllib.request.urlopen(f"{DAEMON}/api/sweeps/pause", data=b"", timeout=30) as r:
                previously = json.loads(r.read()).get("previously_paused")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            sys.exit(f"could not pause sweeps ({exc}); --no-pause proceeds without it")
    try:
        yield previously
    finally:
        if enabled and not previously:
            with contextlib.suppress(Exception):
                urllib.request.urlopen(f"{DAEMON}/api/sweeps/resume", data=b"", timeout=30).close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="delete; without it, only report")
    ap.add_argument("--project", action="append", default=[], metavar="PATH",
                    help="limit to this project (repeatable); default every searchable store")
    ap.add_argument("--max-seconds", type=float, default=1800.0,
                    help="stop between stores after this much wall time (default 1800)")
    ap.add_argument("--no-pause", action="store_true", help="skip the sweep pause")
    args = ap.parse_args()

    projects = args.project or searchable_stores(list_projects())
    deadline = time.monotonic() + args.max_seconds
    files = mass_n = missing_n = foreign_n = 0
    failed: list[str] = []
    stopped, verb = None, ("purged" if args.apply else "would purge")
    with sweeps_paused(not args.no_pause):
        for project in projects:
            if time.monotonic() > deadline:
                stopped = project
                break
            db = index_dir(project) / "vectors.db"
            if not db.exists():
                continue
            try:
                mass, hashed = _store_paths_with_mass(db)
                cfg = effective_config(Path(project).resolve())
                doomed, missing, foreign = plan_store(project, set(mass) | hashed, cfg)
                chunks = sum(mass.get(p, 0) for p in doomed)
                if doomed:
                    print(f"{verb} {len(doomed):>6,} files / {chunks:>8,} chunks  {project}",
                          flush=True)
                if doomed and args.apply:
                    with contextlib.closing(VectorStore(db)) as store:
                        for p in doomed:
                            store.delete_by_path(p)
                        store.flush()
            except (sqlite3.Error, OSError) as exc:
                print(f"!! {project}: {exc}", file=sys.stderr)
                failed.append(project)
                continue
            files, mass_n = files + len(doomed), mass_n + chunks
            missing_n, foreign_n = missing_n + missing, foreign_n + foreign

    print(f"\n{verb}: {files:,} files / {mass_n:,} chunks across {len(projects)} stores"
          f"\nleft alone: {missing_n:,} paths gone from disk (the incremental indexer's job),"
          f" {foreign_n:,} outside their own project root")
    if stopped:
        print(f"stopped at the {args.max_seconds:.0f}s bound before {stopped}; re-run to continue")
    if failed:
        print(f"FAILED on {len(failed)} store(s): {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
