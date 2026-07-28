"""Index a project: discover -> chunk -> batch embed -> store."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import numpy as np

from rag_search.core.config import embed_batch_size
from rag_search.index.chunker import Chunk, chunk_file
from rag_search.index.discover import detect_language, iter_files
from rag_search.index.store import VectorStore

log = logging.getLogger(__name__)


def _chunk_id(path: str, position: int) -> int:
    return int(hashlib.sha256(f"{path}:{position}".encode()).hexdigest()[:15], 16)


def _content_hash(content: str) -> str:
    """Identity of a file's indexed content, paired with the pipeline that chunked it.

    The embed signature is folded in so a model or chunk-budget change invalidates every
    stored hash instead of letting the skip serve vectors built by the old pipeline.
    """
    from rag_search.index.store import embed_signature

    h = hashlib.sha256(embed_signature().encode())
    h.update(b"\x00")
    h.update(content.encode("utf-8", "replace"))
    return h.hexdigest()


def _thermal_pace(_temp_fn=None, _sleep_fn=None) -> None:
    """Background-only: pause briefly when the GPU is near the hard-raise ceiling.

    Called before each embed batch during bulk indexing so large repos complete
    without triggering embed()'s RuntimeError.  Releases the GIL via sleep so
    the asyncio event loop stays responsive.  Never called on the query path.

    _temp_fn / _sleep_fn: injectable for deterministic tests only, matching the
    seam embedder._await_thermal_headroom already uses for the same guard.
    """
    import time

    from rag_search.core.config import THERMAL_MAX_C
    from rag_search.core.gpu import gpu_temp_c

    gpu_temp_c = _temp_fn if _temp_fn is not None else gpu_temp_c
    time_sleep = _sleep_fn if _sleep_fn is not None else time.sleep

    # 0.25s, not 3s: a batch takes well under a second, so a coarse quantum turns one
    # over-temperature reading into a guaranteed multi-second idle, the card cools, the
    # next batch reheats it, and the gate re-arms. That oscillation — not the threshold —
    # was 57% of the July-2026 migration's embed phase.
    waited = 0.0
    while gpu_temp_c() >= THERMAL_MAX_C - 2 and waited < 120.0:
        time_sleep(0.25)
        waited += 0.25


def index_project(
    project_path: str | Path,
    embedder,
    store: VectorStore,
    *,
    federation_mode: bool = True,
) -> tuple[int, int]:
    """Discover, chunk, embed, and store all files. Returns (files, chunks)."""
    root = Path(project_path)
    batch = embed_batch_size()

    chunks: list[Chunk] = []
    digests: list[tuple[str, str]] = []
    file_count = 0
    for fpath in iter_files(root, federation_mode=federation_mode):
        try:
            content = fpath.read_text(errors="replace")
        except OSError:
            continue
        lang = detect_language(fpath)
        file_chunks = chunk_file(fpath, content, lang, project_root=root)
        chunks.extend(file_chunks)
        digests.append((str(fpath), _content_hash(content)))
        file_count += 1

    if not chunks:
        return 0, 0

    texts = [c.content for c in chunks]
    vectors: list[np.ndarray] = []
    for i in range(0, len(texts), batch):
        _thermal_pace()
        vecs = embedder.embed(texts[i : i + batch], batch_size=batch)
        vectors.extend(vecs)

    store.clear()
    for pos, (chunk, vec) in enumerate(zip(chunks, vectors, strict=True)):
        store.insert(
            chunk_id=_chunk_id(chunk.path, pos),
            path=chunk.path,
            start=chunk.start_line,
            end=chunk.end_line,
            language=chunk.language,
            content=chunk.content,
            vector=vec,
        )
    # Seed the skip index, so the first watcher event after a full reindex re-embeds only
    # what actually changed rather than every file it happens to arrive with.
    for path, digest in digests:
        store.set_file_hash(path, digest)
    store.stamp()
    store.flush()
    return file_count, len(chunks)


def index_files(
    files: list[Path],
    embedder,
    store: VectorStore,
    *,
    project_root: Path | None = None,
) -> tuple[int, int]:
    """Incremental re-index: delete stale chunks for changed paths, embed fresh ones.

    A file whose bytes still hash to what is already embedded is skipped outright. The
    watcher fires on writes, not on content changes, so generators, formatters and
    `git checkout` routinely hand us files that are byte-identical to the indexed copy;
    embedding those again costs full GPU + tokenizer work for a bit-identical vector.
    Unreadable/deleted paths still fall through to delete_by_path so purges keep working.
    """
    unchanged = 0
    pending: list[tuple[Path, str, str]] = []
    for fpath in files:
        try:
            content = fpath.read_text(errors="replace")
        except OSError:
            pending.append((fpath, "", ""))  # gone or unreadable: purge, nothing to embed
            continue
        digest = _content_hash(content)
        if store.file_hash(str(fpath)) == digest:
            unchanged += 1
            continue
        pending.append((fpath, content, digest))
    if unchanged:
        log.info("index_files: %d/%d rewritten byte-identical, re-embed skipped",
                 unchanged, len(files))

    for fpath, _content, _digest in pending:
        store.delete_by_path(str(fpath))
    chunks: list[Chunk] = []
    for fpath, content, digest in pending:
        if not digest:
            continue  # deleted or unreadable: purged above, nothing left to embed
        lang = detect_language(fpath)
        chunks.extend(chunk_file(fpath, content, lang, project_root=project_root))
    batch = embed_batch_size()
    texts = [c.content for c in chunks]
    vectors: list[np.ndarray] = []
    for i in range(0, len(texts), batch):
        _thermal_pace()
        vecs = embedder.embed(texts[i : i + batch], batch_size=batch)
        vectors.extend(vecs)
    for pos, (chunk, vec) in enumerate(zip(chunks, vectors, strict=True)):
        store.insert(
            chunk_id=_chunk_id(chunk.path, pos),
            path=chunk.path,
            start=chunk.start_line,
            end=chunk.end_line,
            language=chunk.language,
            content=chunk.content,
            vector=vec,
        )
    # Only stamped once the chunks above are really in the store, so a crash mid-embed
    # leaves no hash claiming work that never landed.
    for fpath, _content, digest in pending:
        if digest:
            store.set_file_hash(str(fpath), digest)
    store.flush()
    return len(files), len(chunks)


