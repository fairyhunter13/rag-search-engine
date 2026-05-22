"""LanceDB storage backend — Arrow schema byte-for-byte compatible with Rust."""

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import lancedb
import numpy as np
import pyarrow as pa

from opencode_search.config import (
    FTS_THRESHOLD,
    IVF_NUM_PARTITIONS_MAX,
    IVF_NUM_SUB_VECTORS_MAX,
    IVF_NPROBES,
    IVF_PQ_THRESHOLD,
    IVF_REFINE_FACTOR,
    SCHEMA_VERSION,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Arrow schema helpers
# ---------------------------------------------------------------------------

def build_schema(dims: int) -> pa.Schema:
    """Build the Arrow schema for the chunks table.

    Must remain byte-for-byte compatible with the Rust storage.rs schema.
    """
    return pa.schema(
        [
            pa.field("chunk_id",     pa.int64()),
            pa.field("path",         pa.utf8()),
            pa.field("file_hash",    pa.utf8()),
            pa.field("language",     pa.utf8()),
            pa.field("position",     pa.int32()),
            pa.field("content",      pa.utf8()),
            pa.field("content_hash", pa.utf8()),
            pa.field("start_line",   pa.int32()),
            pa.field("end_line",     pa.int32()),
            pa.field("vector",       pa.list_(pa.float32(), dims)),
            pa.field("created_at",   pa.timestamp("us")),
        ]
    )


_CONFIG_SCHEMA = pa.schema(
    [
        pa.field("key",   pa.utf8()),
        pa.field("value", pa.utf8()),
    ]
)

# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class ChunkData:
    """An embedded code chunk ready to be stored."""

    chunk_id: int
    path: str
    file_hash: str
    language: str
    position: int
    content: str
    content_hash: str
    start_line: int
    end_line: int
    vector: list[float]
    created_at: int  # microseconds since UNIX epoch


# ---------------------------------------------------------------------------
# Storage class
# ---------------------------------------------------------------------------

class Storage:
    """Async wrapper around a LanceDB database for semantic code search."""

    TABLE_CHUNKS = "chunks"
    TABLE_CONFIG = "config"

    def __init__(self, db_path: str, dims: int) -> None:
        self.db_path = db_path
        self.dims = dims
        self._db: Any = None
        self._table: Any = None
        self._config_table: Any = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Connect to (or create) the LanceDB database and ensure tables exist."""
        self._db = lancedb.connect(self.db_path)
        await self._ensure_chunks_table()
        await self._ensure_config_table()
        logger.debug("Storage opened at %s (dims=%d)", self.db_path, self.dims)

    async def close(self) -> None:
        """No-op — LanceDB manages connections internally."""

    # ------------------------------------------------------------------
    # Table initialisation
    # ------------------------------------------------------------------

    async def _ensure_chunks_table(self) -> None:
        schema = build_schema(self.dims)
        existing = self._db.table_names()
        if self.TABLE_CHUNKS not in existing:
            self._table = self._db.create_table(
                self.TABLE_CHUNKS,
                schema=schema,
            )
            logger.info("Created chunks table (dims=%d)", self.dims)
        else:
            self._table = self._db.open_table(self.TABLE_CHUNKS)
            # Validate that the stored schema matches expectations.
            stored_schema = self._table.schema
            if stored_schema.field("vector").type != schema.field("vector").type:
                raise ValueError(
                    f"Schema mismatch: stored vector type "
                    f"{stored_schema.field('vector').type} != "
                    f"expected {schema.field('vector').type}"
                )

    async def _ensure_config_table(self) -> None:
        existing = self._db.table_names()
        if self.TABLE_CONFIG not in existing:
            self._config_table = self._db.create_table(
                self.TABLE_CONFIG,
                schema=_CONFIG_SCHEMA,
            )
            # Store schema version immediately.
            self.set_config("schema_version", SCHEMA_VERSION)
        else:
            self._config_table = self._db.open_table(self.TABLE_CONFIG)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def write_chunks(self, chunks: list[ChunkData]) -> None:
        """Upsert a list of ChunkData into the chunks table."""
        if not chunks:
            return

        schema = build_schema(self.dims)

        # Build column-oriented data for PyArrow.
        chunk_ids   = pa.array([c.chunk_id     for c in chunks], type=pa.int64())
        paths       = pa.array([c.path         for c in chunks], type=pa.utf8())
        file_hashes = pa.array([c.file_hash    for c in chunks], type=pa.utf8())
        languages   = pa.array([c.language     for c in chunks], type=pa.utf8())
        positions   = pa.array([c.position     for c in chunks], type=pa.int32())
        contents    = pa.array([c.content      for c in chunks], type=pa.utf8())
        content_hashes = pa.array([c.content_hash for c in chunks], type=pa.utf8())
        start_lines = pa.array([c.start_line   for c in chunks], type=pa.int32())
        end_lines   = pa.array([c.end_line     for c in chunks], type=pa.int32())
        vectors     = pa.array(
            [c.vector for c in chunks],
            type=pa.list_(pa.float32(), self.dims),
        )
        created_ats = pa.array(
            [c.created_at for c in chunks],
            type=pa.timestamp("us"),
        )

        pa_table = pa.table(
            {
                "chunk_id":     chunk_ids,
                "path":         paths,
                "file_hash":    file_hashes,
                "language":     languages,
                "position":     positions,
                "content":      contents,
                "content_hash": content_hashes,
                "start_line":   start_lines,
                "end_line":     end_lines,
                "vector":       vectors,
                "created_at":   created_ats,
            },
            schema=schema,
        )

        (
            self._table
            .merge_insert("chunk_id")
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute(pa_table)
        )
        logger.debug("Wrote %d chunks", len(chunks))

    async def delete_by_path(self, path: str) -> None:
        """Delete all chunks whose path matches the given string."""
        escaped = path.replace("'", "\\'")
        self._table.delete(f"path = '{escaped}'")
        logger.debug("Deleted chunks for path: %s", path)

    async def delete_by_paths(self, paths: list[str]) -> None:
        """Delete all chunks whose path is in the given list."""
        if not paths:
            return
        quoted = ", ".join(f"'{p.replace(chr(39), chr(92) + chr(39))}'" for p in paths)
        self._table.delete(f"path IN ({quoted})")
        logger.debug("Deleted chunks for %d paths", len(paths))

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def get_file_hashes(self) -> dict[str, str]:
        """Return a mapping of {path: file_hash} for all indexed files."""
        try:
            df = (
                self._table
                .search()
                .select(["path", "file_hash"])
                .limit(None)
                .to_arrow()
            )
            paths = df["path"].to_pylist()
            hashes = df["file_hash"].to_pylist()
            return dict(zip(paths, hashes))
        except Exception as exc:  # noqa: BLE001
            logger.debug("get_file_hashes error (table may be empty): %s", exc)
            return {}

    async def count(self) -> int:
        """Return the total number of chunks stored."""
        try:
            return self._table.count_rows()
        except Exception:  # noqa: BLE001
            return 0

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search_vector(
        self,
        query_vec: list[float],
        limit: int = 20,
        nprobes: int = IVF_NPROBES,
        refine_factor: int = IVF_REFINE_FACTOR,
    ) -> list[dict]:
        """ANN vector search.  Returns list of row dicts with a '_score' key."""
        query_arr = np.array(query_vec, dtype=np.float32)
        try:
            results = (
                self._table
                .search(query_arr)
                .limit(limit)
                .nprobes(nprobes)
                .refine_factor(refine_factor)
                .to_arrow()
            )
        except Exception:
            # Fall back without IVF params (flat search before index exists).
            results = (
                self._table
                .search(query_arr)
                .limit(limit)
                .to_arrow()
            )

        rows = results.to_pylist()
        # LanceDB returns '_distance'; convert to similarity score (lower = better).
        for row in rows:
            row.setdefault("_score", 1.0 - float(row.get("_distance", 0.0)))
        return rows

    async def search_fts(self, query: str, limit: int = 20) -> list[dict]:
        """Full-text search on the content field.

        Returns list of row dicts with a '_score' key (BM25 relevance).
        """
        try:
            results = (
                self._table
                .search(query, query_type="fts")
                .limit(limit)
                .to_arrow()
            )
            rows = results.to_pylist()
            for row in rows:
                row.setdefault("_score", float(row.get("_relevance_score", 0.0)))
            return rows
        except Exception as exc:  # noqa: BLE001
            logger.debug("FTS search error: %s", exc)
            return []

    async def search_hybrid(
        self,
        query_text: str,
        query_vec: list[float],
        limit: int = 20,
    ) -> list[dict]:
        """Merge vector + FTS results, deduplicate by path keeping highest score."""
        vec_results = await self.search_vector(query_vec, limit=limit)
        fts_results = await self.search_fts(query_text, limit=limit)

        # Deduplicate by path, keeping the row with the highest score.
        seen: dict[str, dict] = {}
        for row in vec_results + fts_results:
            path = row.get("path", "")
            score = row.get("_score", 0.0)
            if path not in seen or score > seen[path].get("_score", 0.0):
                seen[path] = row

        merged = sorted(seen.values(), key=lambda r: r.get("_score", 0.0), reverse=True)
        return merged[:limit]

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    async def ensure_fts_index(self) -> None:
        """Create an FTS index on the content field once >= FTS_THRESHOLD rows exist."""
        n = await self.count()
        if n < FTS_THRESHOLD:
            return
        try:
            self._table.create_fts_index("content", replace=True)
            logger.info("FTS index created/refreshed (%d rows)", n)
        except Exception as exc:  # noqa: BLE001
            logger.warning("FTS index creation failed: %s", exc)

    async def ensure_ivf_pq_index(self) -> None:
        """Create an IVF-PQ ANN index once >= IVF_PQ_THRESHOLD rows exist."""
        n = await self.count()
        if n < IVF_PQ_THRESHOLD:
            return
        n_partitions = min(IVF_NUM_PARTITIONS_MAX, max(1, n // 10))
        n_sub_vectors = min(IVF_NUM_SUB_VECTORS_MAX, max(1, self.dims // 4))
        try:
            self._table.create_index(
                metric="cosine",
                vector_column_name="vector",
                index_type="IVF_PQ",
                num_partitions=n_partitions,
                num_sub_vectors=n_sub_vectors,
                replace=True,
            )
            logger.info(
                "IVF-PQ index created/refreshed (rows=%d, partitions=%d, sub_vectors=%d)",
                n,
                n_partitions,
                n_sub_vectors,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("IVF-PQ index creation failed: %s", exc)

    async def maybe_create_indexes(self) -> None:
        """Ensure both FTS and IVF-PQ indexes are created/updated as warranted."""
        await self.ensure_fts_index()
        await self.ensure_ivf_pq_index()

    # ------------------------------------------------------------------
    # Config key-value store
    # ------------------------------------------------------------------

    def get_config(self, key: str) -> Optional[str]:
        """Retrieve a config value by key, or None if not found."""
        try:
            result = (
                self._config_table
                .search()
                .where(f"key = '{key}'")
                .limit(1)
                .to_arrow()
            )
            rows = result.to_pylist()
            if rows:
                return rows[0].get("value")
        except Exception as exc:  # noqa: BLE001
            logger.debug("get_config(%r) error: %s", key, exc)
        return None

    def set_config(self, key: str, value: str) -> None:
        """Upsert a config key-value pair."""
        pa_table = pa.table(
            {"key": pa.array([key], type=pa.utf8()), "value": pa.array([value], type=pa.utf8())},
            schema=_CONFIG_SCHEMA,
        )
        try:
            (
                self._config_table
                .merge_insert("key")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute(pa_table)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_config(%r, %r) error: %s", key, value, exc)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    async def compact(self) -> None:
        """Compact the chunks table to remove fragmentation."""
        try:
            self._table.compact_files()
            logger.info("Chunks table compacted")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Compaction failed: %s", exc)
