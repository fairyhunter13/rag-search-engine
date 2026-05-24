"""LanceDB storage backend — Arrow schema byte-for-byte compatible with Rust."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

try:
    import lancedb
except ModuleNotFoundError:  # pragma: no cover - exercised via import-time fallback
    lancedb = None

try:
    import numpy as np
except ModuleNotFoundError:  # pragma: no cover - exercised via import-time fallback
    np = None

try:
    import pyarrow as pa
except ModuleNotFoundError:  # pragma: no cover - exercised via import-time fallback
    pa = None

from opencode_search.config import (
    FTS_THRESHOLD,
    IVF_NPROBES,
    IVF_NUM_PARTITIONS_MAX,
    IVF_NUM_SUB_VECTORS_MAX,
    IVF_PQ_THRESHOLD,
    IVF_REFINE_FACTOR,
    SCHEMA_VERSION,
)

logger = logging.getLogger(__name__)


def _missing_dependency_error(package: str) -> ModuleNotFoundError:
    return ModuleNotFoundError(
        f"{package} is required for the LanceDB storage backend. "
        'Install the full runtime with `pip install -e "src/[dev]"`.'
    )


def _require_lancedb():
    if lancedb is None:
        raise _missing_dependency_error("lancedb")
    return lancedb


def _require_numpy():
    if np is None:
        raise _missing_dependency_error("numpy")
    return np


def _require_pyarrow():
    if pa is None:
        raise _missing_dependency_error("pyarrow")
    return pa

# ---------------------------------------------------------------------------
# Arrow schema helpers
# ---------------------------------------------------------------------------

def build_schema(dims: int) -> pa.Schema:
    """Build the Arrow schema for the chunks table.

    Must remain byte-for-byte compatible with the Rust storage.rs schema.
    """
    pa_mod = _require_pyarrow()
    return pa_mod.schema(
        [
            pa_mod.field("chunk_id", pa_mod.int64()),
            pa_mod.field("path", pa_mod.utf8()),
            pa_mod.field("file_hash", pa_mod.utf8()),
            pa_mod.field("language", pa_mod.utf8()),
            pa_mod.field("position", pa_mod.int32()),
            pa_mod.field("content", pa_mod.utf8()),
            pa_mod.field("content_hash", pa_mod.utf8()),
            pa_mod.field("start_line", pa_mod.int32()),
            pa_mod.field("end_line", pa_mod.int32()),
            pa_mod.field("vector", pa_mod.list_(pa_mod.float32(), dims)),
            pa_mod.field("created_at", pa_mod.timestamp("us")),
        ]
    )


_CONFIG_SCHEMA: pa.Schema | None = None


def _get_config_schema() -> pa.Schema:
    global _CONFIG_SCHEMA
    if _CONFIG_SCHEMA is None:
        pa_mod = _require_pyarrow()
        _CONFIG_SCHEMA = pa_mod.schema(
            [
                pa_mod.field("key", pa_mod.utf8()),
                pa_mod.field("value", pa_mod.utf8()),
            ]
        )
    return _CONFIG_SCHEMA

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
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Connect to (or create) the LanceDB database and ensure tables exist."""
        lancedb_mod = _require_lancedb()
        self._db = lancedb_mod.connect(self.db_path)
        await self._ensure_chunks_table()
        await self._ensure_config_table()
        logger.debug("Storage opened at %s (dims=%d)", self.db_path, self.dims)

    async def close(self) -> None:
        """No-op — LanceDB manages connections internally."""

    # ------------------------------------------------------------------
    # Table initialisation
    # ------------------------------------------------------------------

    def _list_tables(self) -> list[str]:
        """Return existing table names.

        Modern LanceDB (>=0.21) exposes `list_tables()` which returns a
        TablesResponse with a `.tables` attribute. Older versions used
        `table_names()` (deprecated). Handles both shapes.
        """
        list_tables = getattr(self._db, "list_tables", None)
        if callable(list_tables):
            resp = list_tables()
            # New API returns a TablesResponse-like object; unwrap .tables.
            tables = getattr(resp, "tables", None)
            if tables is not None:
                return list(tables)
            # Some older 0.20.x versions returned a list directly.
            return list(resp)
        return list(self._db.table_names())

    async def _ensure_chunks_table(self) -> None:
        schema = build_schema(self.dims)
        existing = self._list_tables()
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
        existing = self._list_tables()
        if self.TABLE_CONFIG not in existing:
            self._config_table = self._db.create_table(
                self.TABLE_CONFIG,
                schema=_get_config_schema(),
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

        pa_mod = _require_pyarrow()
        schema = build_schema(self.dims)

        # Build column-oriented data for PyArrow.
        chunk_ids = pa_mod.array([c.chunk_id for c in chunks], type=pa_mod.int64())
        paths = pa_mod.array([c.path for c in chunks], type=pa_mod.utf8())
        file_hashes = pa_mod.array([c.file_hash for c in chunks], type=pa_mod.utf8())
        languages = pa_mod.array([c.language for c in chunks], type=pa_mod.utf8())
        positions = pa_mod.array([c.position for c in chunks], type=pa_mod.int32())
        contents = pa_mod.array([c.content for c in chunks], type=pa_mod.utf8())
        content_hashes = pa_mod.array([c.content_hash for c in chunks], type=pa_mod.utf8())
        start_lines = pa_mod.array([c.start_line for c in chunks], type=pa_mod.int32())
        end_lines = pa_mod.array([c.end_line for c in chunks], type=pa_mod.int32())
        vectors = pa_mod.array(
            [c.vector for c in chunks],
            type=pa_mod.list_(pa_mod.float32(), self.dims),
        )
        created_ats = pa_mod.array(
            [c.created_at for c in chunks],
            type=pa_mod.timestamp("us"),
        )

        pa_table = pa_mod.table(
            {
                "chunk_id": chunk_ids,
                "path": paths,
                "file_hash": file_hashes,
                "language": languages,
                "position": positions,
                "content": contents,
                "content_hash": content_hashes,
                "start_line": start_lines,
                "end_line": end_lines,
                "vector": vectors,
                "created_at": created_ats,
            },
            schema=schema,
        )

        async with self._write_lock:
            (
                self._table
                .merge_insert("chunk_id")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute(pa_table)
            )
        logger.debug("Wrote %d chunks", len(chunks))

    @staticmethod
    def _sql_quote(value: str) -> str:
        """Quote a string literal for LanceDB/DataFusion SQL predicates."""
        return "'" + value.replace("'", "''") + "'"

    async def delete_by_path(self, path: str) -> None:
        """Delete all chunks whose path matches the given string."""
        async with self._write_lock:
            self._table.delete(f"path = {self._sql_quote(path)}")
        logger.debug("Deleted chunks for path: %s", path)

    async def delete_by_paths(self, paths: list[str]) -> None:
        """Delete all chunks whose path is in the given list."""
        if not paths:
            return
        quoted = ", ".join(self._sql_quote(p) for p in paths)
        async with self._write_lock:
            self._table.delete(f"path IN ({quoted})")
        logger.debug("Deleted chunks for %d paths", len(paths))

    async def delete_positions_at_or_after(self, path: str, position: int) -> None:
        """Delete chunks for path whose position is greater than or equal to position."""
        async with self._write_lock:
            self._table.delete(f"path = {self._sql_quote(path)} AND position >= {int(position)}")
        logger.debug("Deleted stale chunks for path %s at position >= %d", path, position)

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
        np_mod = _require_numpy()
        query_arr = np_mod.array(query_vec, dtype=np_mod.float32)
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
        # LanceDB returns '_distance' (lower = closer). Convert to similarity
        # score and clamp to [0, 1] — cosine distance ranges [0, 2] so naive
        # `1 - d` can go negative for poor matches.
        for row in rows:
            d = float(row.get("_distance", 0.0))
            row.setdefault("_score", max(0.0, min(1.0, 1.0 - d)))
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
                # LanceDB BM25 scores are not on the same scale as vector similarity.
                # Normalize to [0, 1) dynamically so hybrid merging is well-behaved
                # without any query-specific heuristics.
                raw = float(row.get("_relevance_score", 0.0) or 0.0)
                row["_fts_score_raw"] = raw
                row["_score"] = raw / (raw + 1.0) if raw > 0.0 else 0.0
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
        """Merge vector + FTS results, deduplicate by chunk keeping highest score."""
        vec_results = await self.search_vector(query_vec, limit=limit)
        fts_results = await self.search_fts(query_text, limit=limit)

        # Deduplicate by chunk, keeping the row with the highest score. Path-level
        # dedup hides multiple relevant chunks from the same file.
        seen: dict[object, dict] = {}
        for row in vec_results + fts_results:
            key = (
                row.get("chunk_id")
                if row.get("chunk_id") is not None
                else (row.get("path", ""), row.get("position", 0))
            )
            score = row.get("_score", 0.0)
            if key not in seen or score > seen[key].get("_score", 0.0):
                seen[key] = row

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

    def get_config(self, key: str) -> str | None:
        """Retrieve a config value by key, or None if not found."""
        try:
            result = (
                self._config_table
                .search()
                .where(f"key = {self._sql_quote(key)}")
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
        pa_mod = _require_pyarrow()
        pa_table = pa_mod.table(
            {
                "key": pa_mod.array([key], type=pa_mod.utf8()),
                "value": pa_mod.array([value], type=pa_mod.utf8()),
            },
            schema=_get_config_schema(),
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
        """Compact the chunks table to remove fragmentation.

        Prefers `Table.optimize()` (new API since LanceDB 0.21). Falls back to
        the deprecated `compact_files()` for older installations.
        """
        try:
            optimize = getattr(self._table, "optimize", None)
            if callable(optimize):
                async with self._write_lock:
                    optimize()
            else:
                async with self._write_lock:
                    self._table.compact_files()
            logger.info("Chunks table compacted")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Compaction failed: %s", exc)
