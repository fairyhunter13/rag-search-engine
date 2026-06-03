"""LanceDB storage backend — Arrow schema byte-for-byte compatible with Rust."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

# lancedb, numpy, and pyarrow are imported lazily (inside _require_* helpers below)
# so that importing storage.py does NOT pull in lancedb's Rust extension at module
# level.  Eager import caused SIGSEGV on Blackwell RTX 5080: lancedb's background
# thread corrupts the glibc heap when the daemon concurrently holds lance files
# open while the test process has lancedb imported but idle.
lancedb = None  # set by _require_lancedb() on first use
np = None       # set by _require_numpy() on first use
pa = None       # set by _require_pyarrow() on first use

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
    global lancedb
    if lancedb is None:
        try:
            import lancedb as _lancedb
            lancedb = _lancedb
        except ModuleNotFoundError as err:
            raise _missing_dependency_error("lancedb") from err
    return lancedb


def _require_numpy():
    global np
    if np is None:
        try:
            import numpy as _np
            np = _np
        except ModuleNotFoundError as err:
            raise _missing_dependency_error("numpy") from err
    return np


def _require_pyarrow():
    global pa
    if pa is None:
        try:
            import pyarrow as _pa
            pa = _pa
        except ModuleNotFoundError as err:
            raise _missing_dependency_error("pyarrow") from err
    return pa

# ---------------------------------------------------------------------------
# Arrow schema helpers
# ---------------------------------------------------------------------------

def build_schema(dims: int, vec_dtype: str = "float16") -> pa.Schema:
    """Build the Arrow schema for the chunks table.

    New tables default to float16 vectors (49% smaller than float32, same
    search quality at cosine similarity).  Existing float32 tables are opened
    as-is for backwards compatibility — the stored dtype is detected in
    _ensure_chunks_table and passed here only for new-table creation.
    """
    pa_mod = _require_pyarrow()
    item_type = pa_mod.float16() if vec_dtype == "float16" else pa_mod.float32()
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
            pa_mod.field("vector", pa_mod.list_(item_type, dims)),
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
        self._vec_dtype: str = "float16"  # updated on open() to match stored schema

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
        existing = self._list_tables()
        if self.TABLE_CHUNKS not in existing:
            # New table: create with float16 (default)
            schema = build_schema(self.dims, vec_dtype="float16")
            self._table = self._db.create_table(self.TABLE_CHUNKS, schema=schema)
            self._vec_dtype = "float16"
            logger.info("Created chunks table (dims=%d, dtype=float16)", self.dims)
        else:
            self._table = self._db.open_table(self.TABLE_CHUNKS)
            stored_schema = self._table.schema
            stored_vec_type = stored_schema.field("vector").type
            # Detect stored dims via list_size (FixedSizeList) or length property
            stored_dims = getattr(stored_vec_type, "list_size", None) or getattr(stored_vec_type, "length", None)
            if stored_dims != self.dims:
                # Dim mismatch (e.g. old 512-dim budget tier vs new 768-dim model).
                # Drop and recreate with float16 schema.
                logger.warning(
                    "Dim mismatch: stored dims %s != expected %d; recreating table.",
                    stored_dims, self.dims,
                )
                self._db.drop_table(self.TABLE_CHUNKS)
                schema = build_schema(self.dims, vec_dtype="float16")
                self._table = self._db.create_table(self.TABLE_CHUNKS, schema=schema)
                self._vec_dtype = "float16"
                logger.info("Recreated chunks table (dims=%d, dtype=float16)", self.dims)
            else:
                # Same dims: honour existing dtype for backwards compatibility.
                # float32 tables written before Jun 2026 remain float32 until
                # re-indexed; new float16 tables stay float16.
                item_type = getattr(stored_vec_type, "value_type", None)
                if item_type is not None and str(item_type) == "float":
                    self._vec_dtype = "float32"
                elif item_type is not None and str(item_type) == "halffloat":
                    self._vec_dtype = "float16"
                else:
                    # Unknown — default to float32 for safety
                    self._vec_dtype = "float32"
                logger.debug(
                    "Opened chunks table (dims=%d, dtype=%s)", self.dims, self._vec_dtype
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
        schema = build_schema(self.dims, vec_dtype=self._vec_dtype)

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
        # Fast path: if vectors are numpy arrays (from GPU batcher), stack them
        # into a contiguous matrix and build a single PyArrow flat buffer — avoids
        # creating O(N·dims) Python float objects that pa.array() with a list of
        # lists would require.
        # Use the stored table's dtype (float16 for new tables, float32 for legacy).
        _np_dtype = "float16" if self._vec_dtype == "float16" else "float32"
        _pa_item = pa_mod.float16() if self._vec_dtype == "float16" else pa_mod.float32()
        try:
            import numpy as _np
            _dtype = _np.float16 if _np_dtype == "float16" else _np.float32
            mat = _np.stack([_np.asarray(c.vector, dtype=_dtype) for c in chunks])
            flat = pa_mod.array(mat.ravel(), type=_pa_item)
            vectors = pa_mod.FixedSizeListArray.from_arrays(flat, self.dims)
        except Exception:
            vectors = pa_mod.array(
                [c.vector for c in chunks],
                type=pa_mod.list_(_pa_item, self.dims),
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

        op = (
            self._table
            .merge_insert("chunk_id")
            .when_matched_update_all()
            .when_not_matched_insert_all()
        )
        async with self._write_lock:
            await asyncio.to_thread(op.execute, pa_table)
        logger.debug("Wrote %d chunks", len(chunks))

    async def append_chunks(self, chunks: list[ChunkData]) -> None:
        """Append chunks without merge/scan — use only after clear() wipes old data."""
        if not chunks:
            return
        pa_mod = _require_pyarrow()
        schema = build_schema(self.dims, vec_dtype=self._vec_dtype)
        chunk_ids = pa_mod.array([c.chunk_id for c in chunks], type=pa_mod.int64())
        paths = pa_mod.array([c.path for c in chunks], type=pa_mod.utf8())
        file_hashes = pa_mod.array([c.file_hash for c in chunks], type=pa_mod.utf8())
        languages = pa_mod.array([c.language for c in chunks], type=pa_mod.utf8())
        positions = pa_mod.array([c.position for c in chunks], type=pa_mod.int32())
        contents = pa_mod.array([c.content for c in chunks], type=pa_mod.utf8())
        content_hashes = pa_mod.array([c.content_hash for c in chunks], type=pa_mod.utf8())
        start_lines = pa_mod.array([c.start_line for c in chunks], type=pa_mod.int32())
        end_lines = pa_mod.array([c.end_line for c in chunks], type=pa_mod.int32())
        _np_dtype = "float16" if self._vec_dtype == "float16" else "float32"
        _pa_item = pa_mod.float16() if self._vec_dtype == "float16" else pa_mod.float32()
        try:
            import numpy as _np
            _dtype = _np.float16 if _np_dtype == "float16" else _np.float32
            mat = _np.stack([_np.asarray(c.vector, dtype=_dtype) for c in chunks])
            flat = pa_mod.array(mat.ravel(), type=_pa_item)
            vectors = pa_mod.FixedSizeListArray.from_arrays(flat, self.dims)
        except Exception:
            vectors = pa_mod.array(
                [c.vector for c in chunks],
                type=pa_mod.list_(_pa_item, self.dims),
            )
        created_ats = pa_mod.array([c.created_at for c in chunks], type=pa_mod.timestamp("us"))
        pa_table = pa_mod.table(
            {
                "chunk_id": chunk_ids, "path": paths, "file_hash": file_hashes,
                "language": languages, "position": positions, "content": contents,
                "content_hash": content_hashes, "start_line": start_lines,
                "end_line": end_lines, "vector": vectors, "created_at": created_ats,
            },
            schema=schema,
        )
        async with self._write_lock:
            await asyncio.to_thread(self._table.add, pa_table)
        logger.debug("Appended %d chunks", len(chunks))

    async def clear(self) -> None:
        """Delete all rows from the chunks table (used before a force re-index)."""
        async with self._write_lock:
            await asyncio.to_thread(self._table.delete, "chunk_id IS NOT NULL")
        logger.info("Chunks table cleared")

    @staticmethod
    def _sql_quote(value: str) -> str:
        """Quote a string literal for LanceDB/DataFusion SQL predicates."""
        return "'" + value.replace("'", "''") + "'"

    async def delete_by_path(self, path: str) -> None:
        """Delete all chunks whose path matches the given string."""
        predicate = f"path = {self._sql_quote(path)}"
        async with self._write_lock:
            await asyncio.to_thread(self._table.delete, predicate)
        logger.debug("Deleted chunks for path: %s", path)

    async def delete_by_paths(self, paths: list[str]) -> None:
        """Delete all chunks whose path is in the given list."""
        if not paths:
            return
        quoted = ", ".join(self._sql_quote(p) for p in paths)
        predicate = f"path IN ({quoted})"
        async with self._write_lock:
            await asyncio.to_thread(self._table.delete, predicate)
        logger.debug("Deleted chunks for %d paths", len(paths))

    async def delete_positions_at_or_after(self, path: str, position: int) -> None:
        """Delete chunks for path whose position is greater than or equal to position."""
        predicate = f"path = {self._sql_quote(path)} AND position >= {int(position)}"
        async with self._write_lock:
            await asyncio.to_thread(self._table.delete, predicate)
        logger.debug("Deleted stale chunks for path %s at position >= %d", path, position)

    async def batch_cleanup_positions(self, cleanups: list[tuple[str, int]]) -> None:
        """Batch-delete stale positions for multiple files in one transaction."""
        if not cleanups:
            return
        parts = [
            f"(path = {self._sql_quote(p)} AND position >= {int(pos)})"
            for p, pos in cleanups
        ]
        predicate = " OR ".join(parts)
        async with self._write_lock:
            await asyncio.to_thread(self._table.delete, predicate)
        logger.debug("Batch-cleaned stale positions for %d files", len(cleanups))

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
            return dict(zip(paths, hashes, strict=False))
        except Exception as exc:
            logger.debug("get_file_hashes error (table may be empty): %s", exc)
            return {}

    async def count(self) -> int:
        """Return the total number of chunks stored."""
        try:
            return self._table.count_rows()
        except Exception:
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

    async def search_vector_language(
        self,
        query_vec: list[float],
        language: str,
        limit: int = 20,
    ) -> list[dict]:
        """Vector search pre-filtered to a specific language value.

        Uses LanceDB's prefilter to search only within matching rows — much
        more effective than post-filtering when the target language is a small
        fraction of the total index (e.g. wiki pages among code chunks).
        """
        np_mod = _require_numpy()
        query_arr = np_mod.array(query_vec, dtype=np_mod.float32)
        where_expr = f"language = '{language}'"
        try:
            results = (
                self._table
                .search(query_arr)
                .where(where_expr, prefilter=True)
                .limit(limit)
                .to_arrow()
            )
        except Exception:
            # Older LanceDB or no prefilter support — fall back to post-filter
            try:
                all_rows = await self.search_vector(query_vec, limit=limit * 10)
                results_list = [r for r in all_rows if r.get("language") == language][:limit]
                return results_list
            except Exception as exc2:
                logger.debug("search_vector_language fallback failed: %s", exc2)
                return []

        rows = results.to_pylist()
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
        except Exception as exc:
            logger.debug("FTS search error: %s", exc)
            return []

    async def search_hybrid(
        self,
        query_text: str,
        query_vec: list[float],
        limit: int = 20,
    ) -> list[dict]:
        """Merge vector + FTS results using Reciprocal Rank Fusion (RRF).

        RRF score = sum(1 / (k + rank_i)) across result lists.
        Chunks that rank well in *both* signals score higher than those
        that dominate only one, without requiring score-scale calibration.
        """
        vec_results = await self.search_vector(query_vec, limit=limit)
        fts_results = await self.search_fts(query_text, limit=limit)

        _k = 60  # standard RRF constant (Cormack et al. 2009)

        rows_by_key: dict[object, dict] = {}
        rrf_scores: dict[object, float] = {}

        for rank, row in enumerate(vec_results):
            key = (
                row.get("chunk_id")
                if row.get("chunk_id") is not None
                else (row.get("path", ""), row.get("position", 0))
            )
            rows_by_key[key] = row
            rrf_scores[key] = rrf_scores.get(key, 0.0) + 1.0 / (_k + rank + 1)

        for rank, row in enumerate(fts_results):
            key = (
                row.get("chunk_id")
                if row.get("chunk_id") is not None
                else (row.get("path", ""), row.get("position", 0))
            )
            if key not in rows_by_key:
                rows_by_key[key] = row
            rrf_scores[key] = rrf_scores.get(key, 0.0) + 1.0 / (_k + rank + 1)

        merged = []
        for key, rrf in rrf_scores.items():
            row = dict(rows_by_key[key])
            row["_score"] = rrf
            merged.append(row)

        merged.sort(key=lambda r: r.get("_score", 0.0), reverse=True)
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
            table = self._table
            await asyncio.to_thread(table.create_fts_index, "content", replace=True)
            logger.info("FTS index created/refreshed (%d rows)", n)
        except Exception as exc:
            logger.warning("FTS index creation failed: %s", exc)

    async def ensure_ivf_pq_index(self) -> None:
        """Create an ANN index once >= IVF_PQ_THRESHOLD rows exist.

        Prefers IVF_HNSW_SQ (IVF + HNSW graph + int8 scalar quantization):
        - int8 quantization cuts index RAM by ~4× vs float32 IVF_FLAT
        - HNSW navigation gives better recall/latency than pure IVF probing
        - Falls back to legacy IVF_PQ for older LanceDB installations
        """
        n = await self.count()
        if n < IVF_PQ_THRESHOLD:
            return
        n_partitions = min(IVF_NUM_PARTITIONS_MAX, max(1, n // 10))
        table = self._table
        # Try IVF_HNSW_SQ first (available since LanceDB 0.12, preferred Jun 2026)
        try:
            await asyncio.to_thread(
                table.create_index,
                metric="cosine",
                vector_column_name="vector",
                index_type="IVF_HNSW_SQ",
                num_partitions=n_partitions,
                replace=True,
            )
            logger.info(
                "IVF_HNSW_SQ index created/refreshed (rows=%d, partitions=%d)",
                n, n_partitions,
            )
            return
        except Exception as exc:
            logger.debug("IVF_HNSW_SQ unavailable, falling back to IVF_PQ: %s", exc)
        # Legacy fallback: IVF_PQ
        n_sub_vectors = min(IVF_NUM_SUB_VECTORS_MAX, max(1, self.dims // 4))
        while n_sub_vectors > 1 and self.dims % n_sub_vectors != 0:
            n_sub_vectors -= 1
        try:
            await asyncio.to_thread(
                table.create_index,
                metric="cosine",
                vector_column_name="vector",
                index_type="IVF_PQ",
                num_partitions=n_partitions,
                num_sub_vectors=n_sub_vectors,
                replace=True,
            )
            logger.info(
                "IVF-PQ index created/refreshed (rows=%d, partitions=%d, sub_vectors=%d)",
                n, n_partitions, n_sub_vectors,
            )
        except Exception as exc:
            logger.warning("Vector index creation failed: %s", exc)

    async def maybe_create_indexes(self) -> None:
        """Compact then ensure both FTS and IVF-PQ indexes are created/updated."""
        await self.compact()
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
        except Exception as exc:
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
        except Exception as exc:
            logger.warning("set_config(%r, %r) error: %s", key, value, exc)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    async def compact(self) -> None:
        """Compact the chunks table to remove fragmentation.

        Calls `Table.optimize()` (LanceDB 0.21+) which merges small fragment
        files AND prunes old dataset versions older than 1 day.  Version pruning
        is the largest storage win: every merge_insert creates a new version;
        without pruning these accumulate indefinitely.  Falls back to the
        deprecated `compact_files()` for older installations.
        """
        try:
            from datetime import timedelta
            table = self._table
            optimize = getattr(table, "optimize", None)
            if callable(optimize):
                async with self._write_lock:
                    await asyncio.to_thread(
                        optimize, cleanup_older_than=timedelta(days=1)
                    )
            else:
                async with self._write_lock:
                    await asyncio.to_thread(table.compact_files)
            logger.info("Chunks table compacted (versions pruned)")
        except Exception as exc:
            logger.warning("Compaction failed: %s", exc)

    async def compact_before_index(self, txn_threshold: int = 500) -> None:
        """Compact if the transaction log is large enough to cause memory pressure.

        Opening a LanceDB table with thousands of tiny transaction files loads all
        of them into memory. Compacting first merges them into a few data files,
        dramatically reducing memory overhead during a subsequent index run.
        Only compacts when the txn count exceeds *txn_threshold* to avoid
        adding unnecessary latency for small/fresh databases.
        """
        import os as _os
        txn_dir = _os.path.join(self.db_path, "chunks.lance", "_transactions")
        try:
            txn_count = len(_os.listdir(txn_dir)) if _os.path.isdir(txn_dir) else 0
        except OSError:
            txn_count = 0
        if txn_count < txn_threshold:
            logger.debug("compact_before_index: %d txns < threshold, skipping", txn_count)
            return
        logger.info(
            "compact_before_index: %d txns >= %d threshold — compacting first",
            txn_count, txn_threshold,
        )
        await self.compact()

    async def vacuum(self, retention_hours: int = 0) -> dict:
        """Deep storage cleanup: compact fragmented files + prune old versions.

        Uses cleanup_older_than=timedelta(hours=retention_hours). Default is 0
        (keep only current version) for maximum space reclaim. Pass retention_hours=168
        (7 days) if you need rollback safety for concurrent readers.

        Returns a dict with before/after stats.
        """
        import os as _os
        from datetime import timedelta

        def _du(path: str) -> int:
            total = 0
            for root, _, files in _os.walk(path):
                for f in files:
                    try:
                        total += _os.path.getsize(_os.path.join(root, f))
                    except OSError:
                        pass
            return total

        before = _du(self.db_path)
        try:
            table = self._table
            optimize = getattr(table, "optimize", None)
            if callable(optimize):
                async with self._write_lock:
                    await asyncio.to_thread(
                        optimize, cleanup_older_than=timedelta(hours=retention_hours)
                    )
            logger.info("Vacuum complete (retention=%dh)", retention_hours)
        except Exception as exc:
            logger.warning("Vacuum failed: %s", exc)
            return {"status": "error", "error": str(exc)}
        after = _du(self.db_path)
        saved_mb = round((before - after) / 1024 / 1024, 1)
        logger.info("Vacuum: %d MB → %d MB (saved %.1f MB)", before // 1024 // 1024, after // 1024 // 1024, saved_mb)
        return {"status": "ok", "before_mb": round(before / 1024 / 1024, 1), "after_mb": round(after / 1024 / 1024, 1), "saved_mb": saved_mb}
