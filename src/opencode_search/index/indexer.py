"""Index a project: discover -> chunk -> batch embed -> store."""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

from opencode_search.core.config import embed_batch_size
from opencode_search.index.chunker import Chunk, chunk_file
from opencode_search.index.discover import detect_language, iter_files
from opencode_search.index.store import VectorStore


def _chunk_id(path: str, position: int) -> int:
    return int(hashlib.sha256(f"{path}:{position}".encode()).hexdigest()[:15], 16)


def _thermal_pace() -> None:
    """Background-only: pause briefly when the GPU is near the hard-raise ceiling.

    Called before each embed batch during bulk indexing so large repos complete
    without triggering embed()'s RuntimeError.  Releases the GIL via sleep so
    the asyncio event loop stays responsive.  Never called on the query path.
    """
    import time

    from opencode_search.core.config import THERMAL_MAX_C
    from opencode_search.core.gpu import gpu_temp_c

    waited = 0.0
    while gpu_temp_c() >= THERMAL_MAX_C - 2 and waited < 120.0:
        time.sleep(3.0)
        waited += 3.0


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
    file_count = 0
    for fpath in iter_files(root, federation_mode=federation_mode):
        try:
            content = fpath.read_text(errors="replace")
        except OSError:
            continue
        lang = detect_language(fpath)
        file_chunks = chunk_file(fpath, content, lang)
        chunks.extend(file_chunks)
        file_count += 1

    if not chunks:
        return 0, 0

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
    store.flush()
    return file_count, len(chunks)


def index_files(
    files: list[Path],
    embedder,
    store: VectorStore,
) -> tuple[int, int]:
    """Incremental re-index: delete stale chunks for changed paths, embed fresh ones."""
    for fpath in files:
        store.delete_by_path(str(fpath))
    chunks: list[Chunk] = []
    for fpath in files:
        try:
            content = fpath.read_text(errors="replace")
        except OSError:
            continue
        lang = detect_language(fpath)
        chunks.extend(chunk_file(fpath, content, lang))
    if not chunks:
        store.flush()
        return len(files), 0
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
    store.flush()
    return len(files), len(chunks)
