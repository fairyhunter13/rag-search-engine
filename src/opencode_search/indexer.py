"""Indexing pipeline: discover → chunk → embed (GPU) → store (LanceDB)."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from opencode_search.cleaner import remove_stale_chunks
from opencode_search.config import (
    get_tier_dims,
    get_tier_models,
)
from opencode_search.discover import detect_language, iter_files
from opencode_search.storage import ChunkData, Storage

log = logging.getLogger(__name__)


@dataclass
class IndexResult:
    files_indexed: int
    files_unchanged: int
    files_removed: int
    chunks_total: int
    errors: int
    elapsed_s: float


def _hash_file(path: Path) -> str:
    """SHA-256 hash of file content (run in thread pool)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _read_file(path: Path) -> str | None:
    """Read text file, returning None on binary/undecodable content."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _make_chunk_id(path: str, position: int) -> int:
    """Stable chunk ID from (path, position) — same as Rust's approach."""
    raw = f"{path}:{position}"
    return int(hashlib.sha256(raw.encode()).hexdigest()[:16], 16) % (2**62)


async def index_file(
    storage: Storage,
    path: Path,
    *,
    tier: str,
    force: bool = False,
    embed_sem: asyncio.Semaphore | None = None,
    existing_hashes: dict[str, str] | None = None,
) -> dict:
    """Index a single file. Returns status dict with 'status' and 'chunks' keys.

    `existing_hashes` should be the result of `storage.get_file_hashes()` (passed
    in so that a project-wide indexing run only loads it once instead of per-file).
    """
    from opencode_search.chunker import chunk_file
    from opencode_search.embeddings import embed_passages

    embed_model, _ = get_tier_models(tier)
    dims = get_tier_dims(tier)
    if embed_sem is None:
        embed_sem = asyncio.Semaphore(1)

    try:
        file_hash = await asyncio.to_thread(_hash_file, path)
    except Exception as e:
        return {"status": "error", "error": str(e), "chunks": 0}

    # Skip if unchanged (unless force)
    if not force:
        if existing_hashes is None:
            existing_hashes = await storage.get_file_hashes()
        if existing_hashes.get(str(path)) == file_hash:
            return {"status": "unchanged", "chunks": 0}

    content = await asyncio.to_thread(_read_file, path)
    if content is None:
        await storage.delete_by_path(str(path))
        return {"status": "skipped", "chunks": 0}

    language = detect_language(path)
    try:
        chunks = await asyncio.to_thread(chunk_file, content, path)
    except Exception as e:
        log.warning("chunking failed for %s: %s", path, e)
        return {"status": "error", "error": str(e), "chunks": 0}

    if not chunks:
        await storage.delete_by_path(str(path))
        return {"status": "empty", "chunks": 0}

    texts = [c.content for c in chunks]
    try:
        async with embed_sem:
            vectors = await asyncio.to_thread(
                embed_passages, texts, model=embed_model, dimensions=dims
            )
    except Exception as e:
        log.error("embedding failed for %s: %s", path, e)
        return {"status": "error", "error": str(e), "chunks": 0}

    if len(vectors) != len(chunks):
        log.error(
            "embed_passages returned %d vectors for %d chunks at %s",
            len(vectors), len(chunks), path,
        )
        return {
            "status": "error",
            "error": f"embedding vector count mismatch: {len(vectors)} != {len(chunks)}",
            "chunks": 0,
        }

    now_us = int(time.time() * 1_000_000)
    chunk_data = [
        ChunkData(
            chunk_id=_make_chunk_id(str(path), i),
            path=str(path),
            file_hash=file_hash,
            language=language,
            position=i,
            content=c.content,
            content_hash=hashlib.sha256(c.content.encode()).hexdigest()[:16],
            start_line=c.start_line,
            end_line=c.end_line,
            vector=vectors[i],
            created_at=now_us,
        )
        for i, c in enumerate(chunks)
        if i < len(vectors)
    ]

    # Upserting by chunk_id alone leaves stale rows when a file shrinks from N
    # chunks to fewer chunks. Write first so a failed write does not erase the
    # previous good index, then remove positions beyond the new chunk count.
    await storage.write_chunks(chunk_data)
    await storage.delete_positions_at_or_after(str(path), len(chunk_data))
    return {"status": "indexed", "chunks": len(chunk_data)}


async def index_project(
    storage: Storage,
    root: Path,
    *,
    tier: str,
    force: bool = False,
    progress_callback=None,
    embed_workers: int = 2,
    file_workers: int = 8,
) -> IndexResult:
    """Index an entire project directory. GPU-enforced, fully async.

    Args:
        storage: Initialized Storage instance.
        root: Project root directory.
        tier: Model tier (budget/balanced/premium).
        force: Re-index even unchanged files.
        progress_callback: Optional async callable(indexed, total, path) for progress.
        embed_workers: Concurrent embed semaphore slots.
    """
    t_start = time.monotonic()
    embed_sem = asyncio.Semaphore(max(1, embed_workers))
    file_sem = asyncio.Semaphore(max(1, file_workers))

    paths = list(iter_files(root))
    total = len(paths)
    log.info("indexing %d files in %s (tier=%s)", total, root, tier)

    # Load existing hashes ONCE so per-file skip detection is O(1) lookup.
    existing_hashes = await storage.get_file_hashes()
    current_path_set = {str(p) for p in paths}

    result = IndexResult(
        files_indexed=0, files_unchanged=0, files_removed=0,
        chunks_total=0, errors=0, elapsed_s=0.0,
    )

    async def process_file(path: Path, idx: int) -> None:
        async with file_sem:
            r = await index_file(
                storage, path,
                tier=tier, force=force,
                embed_sem=embed_sem,
                existing_hashes=existing_hashes,
            )
        if r["status"] == "indexed":
            result.files_indexed += 1
            result.chunks_total += r["chunks"]
        elif r["status"] == "unchanged":
            result.files_unchanged += 1
        elif r["status"] == "error":
            result.errors += 1
            log.warning("index error %s: %s", path, r.get("error"))
        if progress_callback:
            await progress_callback(idx + 1, total, str(path))

    tasks = [process_file(p, i) for i, p in enumerate(paths)]
    await asyncio.gather(*tasks)

    removed = await remove_stale_chunks(storage, current_path_set)
    result.files_removed = removed

    await storage.maybe_create_indexes()

    result.elapsed_s = time.monotonic() - t_start
    log.info(
        "index complete: %d indexed, %d unchanged, %d removed, %d errors in %.1fs",
        result.files_indexed, result.files_unchanged, result.files_removed,
        result.errors, result.elapsed_s,
    )
    return result


async def index_files(
    storage: Storage,
    paths: list[Path],
    *,
    tier: str,
    embed_workers: int = 2,
    file_workers: int = 8,
) -> IndexResult:
    """Index a specific list of files (used by watcher for incremental updates)."""
    t_start = time.monotonic()
    embed_sem = asyncio.Semaphore(max(1, embed_workers))
    file_sem = asyncio.Semaphore(max(1, file_workers))
    result = IndexResult(0, 0, 0, 0, 0, 0.0)

    # Watchdog can emit duplicate events for the same file in one debounce
    # window. Preserve order while avoiding concurrent replacement of one path.
    paths = list(dict.fromkeys(paths))

    # Reuse hash map across the batch
    existing_hashes = await storage.get_file_hashes()

    async def process(path: Path) -> None:
        async with file_sem:
            r = await index_file(
                storage, path,
                tier=tier,
                embed_sem=embed_sem,
                existing_hashes=existing_hashes,
            )
        if r["status"] == "indexed":
            result.files_indexed += 1
            result.chunks_total += r["chunks"]
        elif r["status"] == "unchanged":
            result.files_unchanged += 1
        elif r["status"] == "error":
            result.errors += 1

    await asyncio.gather(*[process(p) for p in paths])
    result.elapsed_s = time.monotonic() - t_start
    return result
