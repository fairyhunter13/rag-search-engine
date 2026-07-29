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


def index_project(
    project_path: str | Path,
    embedder,
    store: VectorStore,
    *,
    federation_mode: bool = True,
) -> tuple[int, int]:
    """Discover, chunk, embed, and store all files. Returns (files, chunks).

    L3: streamed. Chunks are embedded and written a batch at a time, each file's hash row lands
    as soon as its own chunks are all in, and the transaction is committed there.

    It used to accumulate the whole project first — every chunk's text and every vector in
    memory, `clear()` then insert, one transaction committed at the very end. That made peak RSS
    scale with the largest project (redacted-name-1: 7,693 files) and made the unit of work the
    *project*: a restart rolled the whole pass back. That is the property behind
    `_DRIFT_REPAIR_MAX = 500` and behind the fleet migration that discarded 104 of 202 projects
    when a pause landed mid-walk — not a cap chosen for throughput, a cap chosen because there
    was no smaller resumable unit to offer.

    Committing per batch bounds the loss to the one file straddling the boundary. That file has
    chunks and no hash row, which is exactly the shape `_index_set_drift` already looks for, so
    it is re-indexed on the next pass rather than needing progress state of its own.

    `clear()` went with it: a blanket delete cannot be resumed, and between the delete and the
    flush the project has no index at all. Paths are replaced in place instead, and anything
    discovery no longer yields is purged once the walk has actually produced something — never
    on an empty walk, or a discovery blip would wipe a healthy store.
    """
    root = Path(project_path)
    batch = embed_batch_size()

    buf: list[Chunk] = []
    owed: list[tuple[str, str, int]] = []  # (path, digest, chunk count produced through it)
    produced = inserted = file_count = 0
    seen: set[str] = set()

    def _stamp_settled() -> None:
        """Write hash rows for every file whose chunks are all in the store, and no others."""
        while owed and owed[0][2] <= inserted:
            path, digest, _ = owed.pop(0)
            store.set_file_hash(path, digest)

    def _drain(force: bool = False) -> None:
        nonlocal inserted
        while buf and (force or len(buf) >= batch):
            take = buf[:batch]
            del buf[:batch]
            vecs = embedder.embed([c.content for c in take], batch_size=batch)
            for chunk, vec in zip(take, vecs, strict=True):
                store.insert(
                    chunk_id=_chunk_id(chunk.path, inserted),
                    path=chunk.path,
                    start=chunk.start_line,
                    end=chunk.end_line,
                    language=chunk.language,
                    content=chunk.content,
                    vector=vec,
                )
                inserted += 1
            # Order is load-bearing: chunks, then the hash rows they justify, then the commit.
            # A commit between the hash row and its chunks would leave a hash claiming work
            # that is not there — the one state `set_file_hash`'s contract forbids.
            _stamp_settled()
            store.flush()

    for fpath in iter_files(root, federation_mode=federation_mode):
        try:
            content = fpath.read_text(errors="replace")
        except OSError:
            continue
        fstr = str(fpath)
        seen.add(fstr)
        store.delete_by_path(fstr)
        lang = detect_language(fpath)
        file_chunks = chunk_file(fpath, content, lang, project_root=root)
        buf.extend(file_chunks)
        produced += len(file_chunks)
        owed.append((fstr, _content_hash(content), produced))
        file_count += 1
        _drain()

    _drain(force=True)
    _stamp_settled()
    if produced:
        for stale in store.indexed_paths() - seen:
            store.delete_by_path(stale)
        store.stamp()
    store.flush()
    return file_count, inserted


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


