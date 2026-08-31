"""A store the reaper removes gets a week of undo before it is gone.

Both fleet wipes surfaced the same way and days late: someone searched a repo
and got nothing back. A store a human typed a command to remove has a witness.
One an automatic path removed has none, which is why moving the wipe onto the
delete event is exactly when this earns its place.

`take` never falls back to `rmtree`. A rename that fails leaves the store where
it stands: a path whose purpose is to be reversible cannot answer a failure by
deleting harder.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

DIR_NAME = ".trash"


def trash_dir() -> Path:
    """Resolved per call, because the tests move `INDEX_DIR` under them."""
    return config.INDEX_DIR / DIR_NAME


def take(store: Path | str) -> Path | None:
    """Move one store aside. Returns where it went, or None if it did not move."""
    source = Path(store)
    if not source.is_dir():
        return None
    target = trash_dir() / f"{int(time.time())}-{source.name}"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.rename(target)
    except OSError as err:
        # Across filesystems, or onto a name already taken. Either way the store
        # stays where it is and the caller reports a store it did not remove.
        log.warning("could not quarantine %s: %s", source, err)
        return None
    return target


def expire(*, now: float | None = None) -> list[Path]:
    """Delete quarantined stores past their window. Returns what went."""
    root = trash_dir()
    if not root.is_dir():
        return []
    cutoff = (time.time() if now is None else now) - config.QUARANTINE_DAYS * 86400
    gone: list[Path] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        stamp, _, _ = entry.name.partition("-")
        # A name this did not write is left alone rather than guessed at.
        if not stamp.isdigit() or int(stamp) > cutoff:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        gone.append(entry)
    return gone
