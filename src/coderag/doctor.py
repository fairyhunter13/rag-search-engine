"""`coderag doctor`: what is wrong, and -- only when asked -- what to do about it.

Split out of `cli.py` for its line ceiling. The seam is the repo's own dry-run
convention: the read-only report is the default, the destructive half is a flag
on the same command, and a finding is a non-zero exit.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import config, disk, gpu, prune, quarantine, registry, store


def run(args) -> int:
    if getattr(args, "compact", False):
        return _compact()
    problems = 0
    missing: list[str] = []
    print(f"gpu: {gpu.providers()[0]}, {gpu.free_vram_bytes() // 2**20} MiB free")
    for entry in registry.enabled_projects():
        if not entry.path.is_dir():
            missing.append(entry.key)
            continue
        try:
            counts = store.orphans(store.connect(entry.path, create=False))
        except FileNotFoundError:
            print(f"unindexed {entry.key}")
            continue
        if any(counts.values()):
            print(f"ORPHANS {entry.key}: {counts}")
            problems += 1

    rows = registry.load()
    verdict = {key: prune.verdict(rows[key]) for key in missing if key in rows}
    if getattr(args, "prune", False) and missing:
        # A vanished path whose root still exists is that root's configuration,
        # not garbage. The gate is the claim rather than last_error, which the
        # hourly sweep clears -- gating on it would make --prune depend on where
        # in the hour it ran.
        #
        # The second gate is the device. An unmounted volume leaves its mount
        # point standing, which is exactly the shape a deleted repo has, so only
        # a `deleted` verdict is acted on here.
        gone = [
            key for key in missing if not _root_alive(rows, key) and verdict.get(key) == "deleted"
        ]
        missing = [key for key in missing if key not in set(gone)]
        if gone:
            dropped, released = registry.forget(gone)
            for key in dropped + released:
                moved = quarantine.take(config.index_path(key).parent)
                print(f"forgot {key}" + (" (store quarantined)" if moved else ""))
    for key in missing:
        print(f"MISSING {key} ({verdict.get(key, 'unknown')})")
        problems += 1

    # The other direction, and against every row rather than the enabled ones:
    # unflagging keeps the store, so a disabled row's directory is claimed. What
    # is left over is a store whose row is gone -- 143 of them, 0.46 GiB, that a
    # row-driven walk could not see because there was no row to start from.
    if not getattr(args, "prune", False):
        for found in registry.unclaimed_stores():
            print(f"UNCLAIMED {found.name}: {_size_mib(found)} MiB")
            problems += 1
        print(f"{problems} problem(s)")
        return 1 if problems else 0

    pruned = freed = 0
    for path in quarantine.expire():
        print(f"expired {path.name}")
    with registry.prunable_stores(force=getattr(args, "force", False)) as (candidates, busy):
        # Deleting inside the lock is the point: no row can appear for one of
        # these between the walk that named them and the rmtree that removes it.
        for found in candidates:
            freed += _size_mib(found)
            shutil.rmtree(found)
            pruned += 1
            print(f"pruned {found.name}")
        for found in busy:
            print(f"BUSY {found.name}: written to within {config.PRUNE_MIN_IDLE_S}s, kept")
            problems += 1
    print(f"{problems} problem(s), pruned {pruned} store(s), {freed} MiB")
    return 1 if problems else 0


def _compact() -> int:
    """The vector repack and the full VACUUM, over every store. Hand-typed.

    Reported per store rather than as a total: a store that gave nothing back
    was already compact, and a total hides which ones those were.

    The repack runs first and the VACUUM second, because the repack is what
    frees the pages and the VACUUM is what returns them. The blocks are printed
    beside the MiB: they are the number a search actually reads, and a store
    whose file barely moved can still have shed most of what a KNN scans.
    """
    for entry in registry.enabled_projects():
        path = config.index_path(entry.path)
        if not path.exists():
            continue
        before = path.stat().st_size
        conn = store.connect(entry.path, create=False)
        blocks_before, blocks_after = store.repack_vectors(conn)
        disk.compact(conn)
        after = path.stat().st_size
        print(
            f"compacted {entry.key}: {before // 2**20} -> {after // 2**20} MiB, "
            f"{blocks_before} -> {blocks_after} vector blocks"
        )
    return 0


def _root_alive(rows: dict, key: str) -> bool:
    entry = rows.get(key)
    return bool(entry) and any(Path(root).is_dir() for root in entry.roots)


def _size_mib(store_dir: Path) -> int:
    return sum(f.stat().st_size for f in store_dir.rglob("*") if f.is_file()) // 2**20
