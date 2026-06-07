"""Chunk removal helpers for LanceDB (called by watcher on file deletion)."""
from __future__ import annotations

import logging

from opencode_search.storage import Storage

log = logging.getLogger(__name__)


async def remove_chunks_for_paths(storage: Storage, paths: list[str]) -> None:
    """Remove all chunks for the given file paths (called by watcher on delete)."""
    if not paths:
        return
    await storage.delete_by_paths(paths)
    log.info("removed chunks for %d deleted paths", len(paths))
