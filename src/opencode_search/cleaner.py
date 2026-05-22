"""Remove stale chunks from LanceDB (chunks for files no longer on disk)."""
from __future__ import annotations
import asyncio
import logging
from pathlib import Path
from opencode_search.storage import Storage

log = logging.getLogger(__name__)

async def remove_stale_chunks(storage: Storage, current_paths: set[str]) -> int:
    """Delete chunks whose path is not in current_paths. Returns count removed."""
    all_hashes = await storage.get_file_hashes()
    stale = [p for p in all_hashes if p not in current_paths]
    if not stale:
        return 0
    await storage.delete_by_paths(stale)
    log.info("removed stale chunks for %d paths", len(stale))
    return len(stale)

async def remove_chunks_for_paths(storage: Storage, paths: list[str]) -> None:
    """Remove all chunks for the given file paths (called by watcher on delete)."""
    if not paths:
        return
    await storage.delete_by_paths(paths)
    log.info("removed chunks for %d deleted paths", len(paths))
