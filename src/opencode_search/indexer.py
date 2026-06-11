"""Indexing pipeline: discover → chunk → embed (GPU) → store (LanceDB)."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencode_search.config import DEFAULT_DIMS, DEFAULT_EMBED_MODEL
from opencode_search.discover import detect_language, iter_files
from opencode_search.storage import ChunkData, Storage

log = logging.getLogger(__name__)

# GPU temperature threshold above which we yield to let the card cool.
# Default 80°C — keeps fans silent on most laptop GPUs (RTX 5080 starts
# ramping fans around 83–85°C). Override via OPENCODE_EMBED_MAX_TEMP.
_MAX_GPU_TEMP: int = int(os.environ.get("OPENCODE_EMBED_MAX_TEMP", "80"))


async def _thermal_yield() -> None:
    """Pause indexing if GPU is running hot to keep fans quiet.

    Reads temperature via pynvml (fast, no subprocess). Sleep duration scales
    linearly with excess degrees: 0.3 s per degree over threshold, capped at 10 s.
    At threshold or below: no sleep (returns immediately).
    """
    from opencode_search.embeddings import _get_gpu_temp_c
    temp = _get_gpu_temp_c()
    if temp is None or temp <= _MAX_GPU_TEMP:
        return
    excess = temp - _MAX_GPU_TEMP
    delay = min(10.0, excess * 0.3)
    log.debug("thermal yield: GPU=%d°C (threshold=%d°C), sleeping %.1fs", temp, _MAX_GPU_TEMP, delay)
    await asyncio.sleep(delay)


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


@dataclass
class _DeferredWrite:
    chunks: list[ChunkData]
    path: str
    chunk_count: int


@dataclass
class _FileReady:
    """A file that has been read and chunked, ready for GPU embedding."""
    path: str
    file_hash: str
    language: str
    chunks: list  # list of ChunkResult from chunker


class _WriteBuffer:
    """Collects chunks from multiple files and flushes to storage in batches.

    When *append_mode* is True the buffer uses storage.append_chunks() (pure
    table.add — no full-table scan) instead of write_chunks() (merge_insert).
    Use append_mode only after storage.clear() has wiped old data; it skips
    the stale-position cleanup step entirely since there are no stale rows.
    """

    def __init__(self, storage: Storage, batch_files: int = 200, *, append_mode: bool = False):
        self._storage = storage
        self._batch_files = batch_files
        self._append_mode = append_mode
        self._pending: list[_DeferredWrite] = []
        self._lock = asyncio.Lock()
        self._total_written = 0

    async def add(self, write: _DeferredWrite) -> None:
        do_flush = False
        async with self._lock:
            self._pending.append(write)
            if len(self._pending) >= self._batch_files:
                do_flush = True
        if do_flush:
            await self.flush()

    async def flush(self) -> None:
        async with self._lock:
            if not self._pending:
                return
            batch = self._pending
            self._pending = []

        all_chunks: list[ChunkData] = []
        for w in batch:
            all_chunks.extend(w.chunks)

        if self._append_mode:
            await self._storage.append_chunks(all_chunks)
        else:
            cleanups: list[tuple[str, int]] = [(w.path, w.chunk_count) for w in batch]
            await self._storage.write_chunks(all_chunks)
            await self._storage.batch_cleanup_positions(cleanups)
        self._total_written += len(batch)
        log.debug("flushed %d files (%d chunks)", len(batch), len(all_chunks))


class _GpuBatcher:
    """Batch-embeds chunks from many files in one large GPU call per batch.

    Accumulates *batch_chunks* text chunks from many files before calling
    embed_passages once. This keeps the GPU fully saturated (one large CUDA
    kernel instead of many tiny ones) while the CPU does minimal work.
    """

    def __init__(
        self,
        storage: Storage,
        embed_model: str,
        dims: int,
        *,
        batch_chunks: int = 256,
        batch_files: int = 50,
        append_mode: bool = False,
    ) -> None:
        self._storage = storage
        self._embed_model = embed_model
        self._dims = dims
        self._batch_chunks = batch_chunks
        self._pending_files: list[_FileReady] = []
        self._pending_texts: list[str] = []
        self._write_buf = _WriteBuffer(storage, batch_files=batch_files, append_mode=append_mode)
        self.total_indexed = 0
        self.total_chunks = 0
        self.errors = 0
        # Per-session content_hash → vector cache: skip re-embedding identical
        # boilerplate chunks (proto-generated code, shared middleware, etc.)
        self._content_vec_cache: dict[str, Any] = {}
        self.total_cache_hits = 0

    async def add(self, fr: _FileReady) -> None:
        self._pending_files.append(fr)
        self._pending_texts.extend(c.content for c in fr.chunks)
        if len(self._pending_texts) >= self._batch_chunks:
            await self._flush()

    async def _flush(self) -> None:
        if not self._pending_files:
            return
        from opencode_search.embeddings import embed_passages
        files, texts = self._pending_files, self._pending_texts
        self._pending_files, self._pending_texts = [], []

        # Build per-chunk content_hashes and partition into cache-hits vs misses
        # so identical boilerplate chunks (proto stubs, shared middleware) are
        # not re-embedded — saves GPU time proportional to dedup rate.
        all_content_hashes = [
            hashlib.sha256(t.encode()).hexdigest()[:16] for t in texts
        ]
        miss_indices: list[int] = []
        miss_texts: list[str] = []
        for idx, (text, ch) in enumerate(zip(texts, all_content_hashes, strict=True)):
            if ch not in self._content_vec_cache:
                miss_indices.append(idx)
                miss_texts.append(text)

        cache_hits = len(texts) - len(miss_texts)
        self.total_cache_hits += cache_hits

        try:
            import numpy as _np
            if miss_texts:
                # Use the dedicated single-thread build executor (not asyncio.to_thread)
                # so passage embeds share one persistent cuBLAS handle and never storm.
                from opencode_search.embeddings import _BUILD_INFER_EXECUTOR
                loop = asyncio.get_event_loop()
                miss_vectors = await loop.run_in_executor(
                    _BUILD_INFER_EXECUTOR,
                    lambda: embed_passages(
                        miss_texts, model=self._embed_model, dimensions=self._dims,
                        _return_numpy=True,
                    ),
                )
                for mi, mv in zip(miss_indices, miss_vectors, strict=True):
                    self._content_vec_cache[all_content_hashes[mi]] = mv
                await _thermal_yield()
            # Build full vectors array (cache hits reuse cached numpy array)
            vectors = _np.stack([
                self._content_vec_cache[ch] for ch in all_content_hashes
            ])
        except Exception as exc:
            log.error("GPU batch embed failed (%d chunks): %s", len(texts), exc)
            self.errors += len(files)
            return

        now_us = int(time.time() * 1_000_000)
        vi = 0
        for fr in files:
            n = len(fr.chunks)
            fv = vectors[vi:vi + n]  # numpy slice — O(1), no copy
            vi += n
            if len(fv) != n:
                log.error("vector mismatch %s: got %d want %d", fr.path, len(fv), n)
                self.errors += 1
                continue
            chunk_data = [
                ChunkData(
                    chunk_id=_make_chunk_id(fr.path, i),
                    path=fr.path,
                    file_hash=fr.file_hash,
                    language=fr.language,
                    position=i,
                    content=c.content,
                    content_hash=all_content_hashes[vi - n + i],
                    start_line=c.start_line,
                    end_line=c.end_line,
                    vector=fv[i],  # 1D numpy array — passed directly to storage
                    created_at=now_us,
                )
                for i, c in enumerate(fr.chunks)
            ]
            await self._write_buf.add(
                _DeferredWrite(chunks=chunk_data, path=fr.path, chunk_count=len(chunk_data))
            )
            self.total_indexed += 1
            self.total_chunks += len(chunk_data)

        if cache_hits:
            log.debug(
                "GPU batch: %d embedded, %d cache hits from %d files",
                len(miss_texts), cache_hits, len(files),
            )
        else:
            log.debug("GPU batch: %d chunks from %d files embedded", len(texts), len(files))

    async def finalize(self) -> None:
        await self._flush()
        await self._write_buf.flush()


async def index_file(
    storage: Storage,
    path: Path,
    *,
    force: bool = False,
    embed_sem: asyncio.Semaphore | None = None,
    existing_hashes: dict[str, str] | None = None,
    project_root: Path | None = None,
    write_buffer: _WriteBuffer | None = None,
) -> dict:
    """Index a single file. Returns status dict with 'status' and 'chunks' keys.

    `existing_hashes` should be the result of `storage.get_file_hashes()` (passed
    in so that a project-wide indexing run only loads it once instead of per-file).
    When *write_buffer* is provided, chunks are deferred into the buffer for
    batched writing instead of being written immediately.
    """
    from opencode_search.chunker import chunk_file
    from opencode_search.embeddings import embed_passages

    embed_model = DEFAULT_EMBED_MODEL
    dims = DEFAULT_DIMS
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
            from opencode_search.embeddings import _BUILD_INFER_EXECUTOR
            loop = asyncio.get_event_loop()
            vectors = await loop.run_in_executor(
                _BUILD_INFER_EXECUTOR,
                lambda: embed_passages(texts, model=embed_model, dimensions=dims),
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

    if write_buffer is not None:
        await write_buffer.add(_DeferredWrite(
            chunks=chunk_data, path=str(path), chunk_count=len(chunk_data),
        ))
    else:
        await storage.write_chunks(chunk_data)
        await storage.delete_positions_at_or_after(str(path), len(chunk_data))
    return {"status": "indexed", "chunks": len(chunk_data)}


async def index_project(
    storage: Storage,
    root: Path,
    *,
    force: bool = False,
    follow_symlinks: bool = True,
    progress_callback=None,
    embed_workers: int = 2,  # kept for API compat; not used in GPU-batch mode
    file_workers: int = 0,  # 0 = auto-scale with batch_chunks
) -> IndexResult:
    """Index an entire project using GPU-batched embedding.

    Files are read and chunked concurrently (bounded by *file_workers*) then
    fed to a single GPU batcher that embeds up to ``batch_chunks`` chunks per
    CUDA call. batch_chunks is auto-sized from VRAM (512 on 16 GB) to keep GPU
    utilisation high while file_workers scales proportionally to keep the reader
    pipeline saturated without wasting CPU on an idle GPU.
    """
    from opencode_search.chunker import chunk_file
    from opencode_search.embeddings import get_embed_batch_chunks

    embed_model = DEFAULT_EMBED_MODEL
    dims = DEFAULT_DIMS
    t_start = time.monotonic()

    batch_chunks = get_embed_batch_chunks()
    # Scale file_workers with batch_chunks so the reader never starves the GPU:
    # 512 batch_chunks → 8 workers; 64 batch_chunks → 4 workers.
    # Cap at 16 to avoid excessive CPU/memory usage.
    if file_workers == 0:
        file_workers = min(16, max(4, batch_chunks // 64))
    file_sem = asyncio.Semaphore(max(1, file_workers))

    paths = list(iter_files(root, follow_symlinks=follow_symlinks))
    total = len(paths)
    log.info("indexing %d files in %s", total, root)

    existing_hashes = await storage.get_file_hashes()
    current_path_set = {str(p) for p in paths}

    # force=True: wipe old data first so the write path can use pure appends
    # (table.add) instead of merge_insert (full-table scan per batch). One
    # clear scan upfront beats 400+ merge scans during indexing.
    if force and existing_hashes:
        await storage.clear()
        existing_hashes = {}

    result = IndexResult(
        files_indexed=0, files_unchanged=0, files_removed=0,
        chunks_total=0, errors=0, elapsed_s=0.0,
    )

    # Bounded queue: readers push _FileReady; embed_writer pulls and batches.
    # maxsize limits how many chunked files sit in memory awaiting embedding.
    ready_queue: asyncio.Queue = asyncio.Queue(maxsize=file_workers * 32)

    async def reader_pool() -> None:
        async def read_one(path: Path, idx: int) -> None:
            async with file_sem:
                try:
                    file_hash = await asyncio.to_thread(_hash_file, path)
                except Exception:
                    result.errors += 1
                    return

                if not force and existing_hashes.get(str(path)) == file_hash:
                    result.files_unchanged += 1
                    if progress_callback:
                        await progress_callback(idx + 1, total, str(path))
                    return

                content = await asyncio.to_thread(_read_file, path)
                if content is None:
                    await storage.delete_by_path(str(path))
                    if progress_callback:
                        await progress_callback(idx + 1, total, str(path))
                    return

                language = detect_language(path)
                try:
                    chunks = await asyncio.to_thread(chunk_file, content, path)
                except Exception as e:
                    log.warning("chunking failed for %s: %s", path, e)
                    result.errors += 1
                    return

                if not chunks:
                    await storage.delete_by_path(str(path))
                    if progress_callback:
                        await progress_callback(idx + 1, total, str(path))
                    return

            # Release file_sem before queueing to avoid holding it during backpressure.
            await ready_queue.put(
                _FileReady(path=str(path), file_hash=file_hash, language=language, chunks=chunks)
            )
            if progress_callback:
                await progress_callback(idx + 1, total, str(path))

        try:
            await asyncio.gather(*[read_one(p, i) for i, p in enumerate(paths)])
        finally:
            await ready_queue.put(None)  # sentinel: all reading done

    async def embed_writer() -> None:
        # batch_files=2000: LanceDB triggers an expensive 63s rebase every 20
        # write transactions. With batch_files=50 → 400 txns → 20 rebases = 21 min
        # of overhead. With batch_files=2000 → ~10 txns → 0 rebases.
        batcher = _GpuBatcher(storage, embed_model, dims, batch_chunks=batch_chunks,
                              batch_files=2000, append_mode=force)
        while True:
            item = await ready_queue.get()
            if item is None:
                break
            await batcher.add(item)
        await batcher.finalize()
        result.files_indexed = batcher.total_indexed
        result.chunks_total = batcher.total_chunks
        result.errors += batcher.errors

    await asyncio.gather(reader_pool(), embed_writer())

    # Compact first so the subsequent stale-hash scan and index builds
    # read a small number of data files, not hundreds of tiny transactions.
    await storage.compact()

    # Reuse the pre-loaded existing_hashes to find stale paths without
    # re-scanning the table (avoids an O(n) Lance scan after 400+ new txns).
    stale = [p for p in existing_hashes if p not in current_path_set]
    if stale:
        await storage.delete_by_paths(stale)
        log.info("removed stale chunks for %d paths", len(stale))
    result.files_removed = len(stale)

    await storage.ensure_fts_index()
    await storage.ensure_ivf_pq_index()

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
    project_root: Path | None = None,
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

    buf = _WriteBuffer(storage, batch_files=50)

    async def process(path: Path) -> None:
        async with file_sem:
            r = await index_file(
                storage, path,
                embed_sem=embed_sem,
                existing_hashes=existing_hashes,
                project_root=project_root,
                write_buffer=buf,
            )
        if r["status"] == "indexed":
            result.files_indexed += 1
            result.chunks_total += r["chunks"]
        elif r["status"] == "unchanged":
            result.files_unchanged += 1
        elif r["status"] == "error":
            result.errors += 1

    await asyncio.gather(*[process(p) for p in paths])
    await buf.flush()
    result.elapsed_s = time.monotonic() - t_start
    return result
