"""Hybrid search with two-stage federated reranking.

Search flow:
  Stage 1 — Per-project hybrid (vector + FTS) retrieval, k=STAGE1_VECTOR_K
  Stage 2 (≤SKIP_STAGE1_RERANK_N projects) — Per-project rerank, k=STAGE1_RERANK_K
           (> SKIP_STAGE1_RERANK_N projects) — Skip per-project rerank; merge all stage-1 results
  Global   — Rerank all merged candidates, return top FINAL_TOP_K

Query embeddings and rerank calls are dispatched to asyncio.to_thread so the
async event loop stays responsive. Both are GPU-only (CPUExecutionProvider is
forbidden — raises GPUNotAvailableError at startup).

LRU result cache (TTLCache) avoids redundant GPU inference for repeated queries.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

try:
    from cachetools import TTLCache
except ModuleNotFoundError:  # pragma: no cover - exercised in dep-light envs
    class TTLCache:
        """Small fallback TTL cache for test/import environments."""

        def __init__(self, maxsize: int, ttl: float) -> None:
            self.maxsize = maxsize
            self.ttl = ttl
            self._entries: OrderedDict[object, tuple[float, object]] = OrderedDict()

        def _purge(self) -> None:
            now = time.monotonic()
            expired = [key for key, (expires_at, _) in self._entries.items() if expires_at <= now]
            for key in expired:
                self._entries.pop(key, None)
            while len(self._entries) > self.maxsize:
                self._entries.popitem(last=False)

        def get(self, key: object, default: object = None) -> object:
            self._purge()
            entry = self._entries.get(key)
            if entry is None:
                return default
            expires_at, value = entry
            if expires_at <= time.monotonic():
                self._entries.pop(key, None)
                return default
            self._entries.move_to_end(key)
            return value

        def __setitem__(self, key: object, value: object) -> None:
            self._entries[key] = (time.monotonic() + self.ttl, value)
            self._entries.move_to_end(key)
            self._purge()

        def clear(self) -> None:
            self._entries.clear()

from opencode_search.config import (
    FINAL_TOP_K,
    GLOBAL_RERANK_MAX,
    SKIP_STAGE1_RERANK_N,
    STAGE1_RERANK_K,
    STAGE1_VECTOR_K,
    ProjectEntry,
    get_tier_dims,
    get_tier_models,
)
from opencode_search.storage import Storage

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class SearchResult:
    """A single ranked result returned to the caller."""

    path: str
    content: str
    language: str
    start_line: int
    end_line: int
    score: float
    project_path: str
    chunk_id: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Query result cache (TTL-based, keyed by query + project fingerprint + params)
# ---------------------------------------------------------------------------

_CACHE_MAXSIZE = int(os.environ.get("OPENCODE_SEARCH_CACHE_SIZE", "128"))
_CACHE_TTL = float(os.environ.get("OPENCODE_SEARCH_CACHE_TTL", "60"))

_result_cache: TTLCache = TTLCache(maxsize=_CACHE_MAXSIZE, ttl=_CACHE_TTL)
# Lock is lazily initialised on first use so it binds to the running event loop.
_cache_lock: asyncio.Lock | None = None


def _get_cache_lock() -> asyncio.Lock:
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


def _cache_key(
    query: str,
    projects: list[ProjectEntry],
    tier: str,
    top_k: int,
    use_rerank: bool,
) -> tuple:
    proj_sig = hashlib.sha256(
        "|".join(
            sorted(
                "::".join(
                    (
                        p.path,
                        p.db_path,
                        p.tier,
                        str(p.dims),
                        str(p.indexed_at),
                        str(p.file_count),
                    )
                )
                for p in projects
            )
        ).encode()
    ).hexdigest()[:16]
    return (query.lower().strip(), proj_sig, tier, top_k, use_rerank)


# ---------------------------------------------------------------------------
# GPU embedding and reranking (sync wrappers dispatched via to_thread)
# ---------------------------------------------------------------------------


def _embed_query_sync(query: str, model: str, dims: int) -> list[float]:
    from opencode_search.embeddings import embed_query
    return embed_query(query, model=model, dimensions=dims)


def _rerank_sync(
    query: str,
    docs: list[str],
    model: str,
    top_k: int,
) -> list[tuple[int, float]]:
    from opencode_search.embeddings import rerank
    return rerank(query, docs, model=model, top_k=top_k)


# ---------------------------------------------------------------------------
# Per-project search helpers
# ---------------------------------------------------------------------------


async def _search_project(
    project: ProjectEntry,
    query: str,
    query_vec: list[float],
    limit: int,
) -> list[dict]:
    """Run hybrid search against one project's LanceDB table."""
    storage = Storage(
        db_path=str(project.db_path),
        dims=get_tier_dims(project.tier),
    )
    try:
        await storage.open()
        rows = await storage.search_hybrid(query, query_vec, limit=limit)
        for row in rows:
            row["_project_path"] = project.path
        return rows
    except Exception as exc:
        log.warning("Search failed for project %s: %s", project.path, exc)
        return []
    finally:
        await storage.close()


async def _rerank_rows(
    query: str,
    rows: list[dict],
    model: str,
    top_k: int,
) -> list[dict]:
    """Rerank a list of row dicts; returns top_k sorted by score descending."""
    if not rows:
        return []

    docs = [row.get("content", "") for row in rows]
    ranked: list[tuple[int, float]] = await asyncio.to_thread(
        _rerank_sync, query, docs, model, top_k
    )

    out: list[dict] = []
    for orig_idx, score in ranked:
        row = dict(rows[orig_idx])
        row["_score"] = score
        out.append(row)

    return out[:top_k]


# ---------------------------------------------------------------------------
# Main search entry point
# ---------------------------------------------------------------------------


async def search(
    query: str,
    *,
    projects: list[ProjectEntry],
    top_k: int = FINAL_TOP_K,
    use_rerank: bool = True,
) -> list[SearchResult]:
    """Federated hybrid search across multiple projects with two-stage reranking.

    Args:
        query:       Natural-language or code search query.
        projects:    List of registered ProjectEntry objects to search across.
        top_k:       Final number of results to return.
        use_rerank:  If False, return raw vector+FTS merged results (faster, less accurate).

    Returns:
        List of SearchResult ordered by relevance (highest score first).
    """
    if not query.strip() or not projects:
        return []

    # All projects must use the same tier for embedding consistency.
    # Use the tier of the first project; mixed-tier federations are unsupported.
    tier = projects[0].tier
    if any(p.tier != tier for p in projects):
        tiers = sorted({p.tier for p in projects})
        raise ValueError(
            "Mixed-tier search is unsupported because embedding dimensions/models differ. "
            f"Requested tiers: {tiers}. Search one tier at a time or re-index projects with the same tier."
        )
    embed_model, rerank_model = get_tier_models(tier)
    dims = get_tier_dims(tier)

    # Cache lookup
    key = _cache_key(query, projects, tier, top_k, use_rerank)
    async with _get_cache_lock():
        cached = _result_cache.get(key)
    if cached is not None:
        log.debug("search cache hit for query=%.40r", query)
        return cached

    t0 = time.perf_counter()

    # Embed the query on GPU
    query_vec: list[float] = await asyncio.to_thread(
        _embed_query_sync, query, embed_model, dims
    )
    if not query_vec:
        log.error("embed_query returned empty vector for query=%.40r", query)
        return []

    # Stage 1 — parallel per-project hybrid retrieval
    tasks = [
        _search_project(proj, query, query_vec, limit=STAGE1_VECTOR_K)
        for proj in projects
    ]
    per_project_results: list[list[dict]] = await asyncio.gather(*tasks)

    n_projects = len(projects)
    candidates: list[dict] = []

    if use_rerank and n_projects <= SKIP_STAGE1_RERANK_N:
        # Stage 2a — per-project rerank, then merge
        rerank_tasks = [
            _rerank_rows(query, rows, rerank_model, STAGE1_RERANK_K)
            for rows in per_project_results
            if rows
        ]
        reranked_per_project: list[list[dict]] = await asyncio.gather(*rerank_tasks)
        for group in reranked_per_project:
            candidates.extend(group)
    else:
        # Stage 2b — merge raw results (no per-project rerank for large federations)
        for rows in per_project_results:
            candidates.extend(rows)

    if not candidates:
        return []

    # Deduplicate by chunk identity keeping highest score. Content-prefix
    # dedup can hide separate chunks that start with common boilerplate.
    seen: dict[tuple, dict] = {}
    for row in candidates:
        dedup_key = (
            row.get("_project_path", ""),
            row.get("chunk_id")
            if row.get("chunk_id") is not None
            else (row.get("path", ""), row.get("position", 0)),
        )
        score = row.get("_score", 0.0)
        if dedup_key not in seen or score > seen[dedup_key].get("_score", 0.0):
            seen[dedup_key] = row
    candidates = list(seen.values())

    # Trim before global rerank to avoid OOM on VRAM
    if len(candidates) > GLOBAL_RERANK_MAX:
        candidates.sort(key=lambda r: r.get("_score", 0.0), reverse=True)
        candidates = candidates[:GLOBAL_RERANK_MAX]

    # Global rerank
    if use_rerank and candidates:
        candidates = await _rerank_rows(query, candidates, rerank_model, top_k)
    else:
        candidates.sort(key=lambda r: r.get("_score", 0.0), reverse=True)
        candidates = candidates[:top_k]

    # Convert to SearchResult
    results = [
        SearchResult(
            path=row.get("path", ""),
            content=row.get("content", ""),
            language=row.get("language", ""),
            start_line=int(row.get("start_line", 0)),
            end_line=int(row.get("end_line", 0)),
            score=float(row.get("_score", 0.0)),
            project_path=row.get("_project_path", ""),
            chunk_id=int(row.get("chunk_id", 0)),
        )
        for row in candidates
    ]

    elapsed = (time.perf_counter() - t0) * 1000
    raw_total = sum(len(r) for r in per_project_results)
    log.info(
        "search[%d projects, %d raw → %d results]: %.0fms",
        n_projects,
        raw_total,
        len(results),
        elapsed,
    )

    # Store in cache
    async with _get_cache_lock():
        _result_cache[key] = results

    return results


# ---------------------------------------------------------------------------
# Single-project convenience wrapper
# ---------------------------------------------------------------------------


async def search_project(
    query: str,
    *,
    project: ProjectEntry,
    top_k: int = FINAL_TOP_K,
    use_rerank: bool = True,
) -> list[SearchResult]:
    """Search a single project. Thin wrapper around `search`."""
    return await search(query, projects=[project], top_k=top_k, use_rerank=use_rerank)


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


def clear_search_cache() -> None:
    """Evict all cached search results (e.g. after re-indexing)."""
    _result_cache.clear()
    log.debug("search result cache cleared")
