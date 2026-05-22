"""LanceDB compaction: merge small files, reduce fragmentation."""
from __future__ import annotations
import asyncio
import logging
from opencode_search.storage import Storage

log = logging.getLogger(__name__)

COMPACTION_THRESHOLD_OPS = 100  # compact after this many write ops

async def compact_if_needed(storage: Storage, ops_since_last: int) -> bool:
    """Run compaction if ops threshold reached. Returns True if compacted."""
    if ops_since_last < COMPACTION_THRESHOLD_OPS:
        return False
    try:
        await asyncio.to_thread(storage.table.compact_files)
        log.info("compaction complete for %s", storage.db_path)
        return True
    except Exception as e:
        log.warning("compaction failed: %s", e)
        return False

async def force_compact(storage: Storage) -> dict:
    """Force compaction regardless of threshold. Returns status dict."""
    try:
        await asyncio.to_thread(storage.table.compact_files)
        return {"status": "ok"}
    except Exception as e:
        log.warning("force compaction failed: %s", e)
        return {"status": "error", "error": str(e)}
