"""Persistent disk-backed answer cache for heavy ask/feature/global/business synthesis.

Mirroring the _service_mesh.py two-tier cache pattern:
- In-process TTL dict (300 s) for sub-millisecond hits within a daemon run.
- On-disk JSON files under <index_dir>/answer_cache/<scope>__<sha1>.json that
  survive daemon restarts and can be warmed by the kb_sweep background loop.

Cache invalidation uses the same `indexed_at + file_count` graph-signature
strategy as search.py:_cache_key.  A file change increments file_count or
advances indexed_at, the signature changes, and stale entries are bypassed
automatically without an explicit delete.

nearest_answer() embeds the query via the same GPU-locked embed_query call used
by the rest of the search pipeline, cosine-matching against stored embeddings so
a semantically-near precomputed card can serve a not-exactly-precomputed query.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_ANSWER_CACHE: dict[str, tuple[float, dict]] = {}  # sha1-key -> (stored_at, entry)
_PROJECT_KEYS: dict[str, set[str]] = {}  # resolved-project-path -> set of sha1-keys
_ANSWER_TTL = 300.0  # 5-min in-process; disk entries survive restarts


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _canonical(query: str) -> str:
    return " ".join(query.lower().split())


def _sha1(project_path: str, scope: str, query: str) -> str:
    raw = f"{project_path}\x00{scope}\x00{_canonical(query)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cache_dir(project_path: str) -> Path:
    from opencode_search.config import get_project_index_dir
    return get_project_index_dir(project_path) / "answer_cache"


def _entry_path(project_path: str, scope: str, query: str) -> Path:
    return _cache_dir(project_path) / f"{scope}__{_sha1(project_path, scope, query)}.json"


def _graph_sig(project_path: str) -> str:
    try:
        from opencode_search.config import load_registry
        reg = load_registry()
        resolved = str(Path(project_path).expanduser().resolve())
        entry = reg.get(resolved)
        if entry is None:
            # try un-resolved
            entry = reg.get(project_path)
        if entry is None:
            return ""
        return f"{entry.indexed_at}::{entry.file_count}"
    except Exception:
        return ""


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def make_answer_key(project_path: str, scope: str, query: str) -> str:
    """Return the in-process cache key for a (project, scope, query) triple."""
    return _sha1(project_path, scope, query)


def load_answer(project_path: str, scope: str, query: str) -> dict[str, Any] | None:
    """Return a cached answer entry, or None on miss or stale graph-signature."""
    key = make_answer_key(project_path, scope, query)

    # 1. In-process cache
    hit = _ANSWER_CACHE.get(key)
    if hit is not None:
        stored_at, entry = hit
        if time.monotonic() - stored_at < _ANSWER_TTL and entry.get("_graph_sig") == _graph_sig(project_path):
            return entry
        _ANSWER_CACHE.pop(key, None)

    # 2. On-disk cache
    path = _entry_path(project_path, scope, query)
    if not path.exists():
        return None
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if entry.get("_graph_sig") != _graph_sig(project_path):
        return None
    resolved = str(Path(project_path).expanduser().resolve())
    _ANSWER_CACHE[key] = (time.monotonic(), entry)
    _PROJECT_KEYS.setdefault(resolved, set()).add(key)
    return entry


def save_answer(
    project_path: str,
    scope: str,
    query: str,
    payload: dict[str, Any],
    *,
    embedding: list[float] | None = None,
) -> None:
    """Persist an answer to the in-process and on-disk cache."""
    key = make_answer_key(project_path, scope, query)
    entry = dict(payload)
    entry["_graph_sig"] = _graph_sig(project_path)
    entry["_cached_at"] = time.time()
    entry["_scope"] = scope
    entry["_query"] = _canonical(query)
    if embedding is not None:
        entry["_embedding"] = embedding
    resolved = str(Path(project_path).expanduser().resolve())
    _ANSWER_CACHE[key] = (time.monotonic(), entry)
    _PROJECT_KEYS.setdefault(resolved, set()).add(key)
    try:
        out_path = _entry_path(project_path, scope, query)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        log.debug("answer_cache: save failed for %s/%s: %s", scope, query[:60], exc)


def invalidate_answers(project_path: str) -> None:
    """Drop all in-process and on-disk cache entries for a project."""
    resolved = str(Path(project_path).expanduser().resolve())
    for key in _PROJECT_KEYS.pop(resolved, set()):
        _ANSWER_CACHE.pop(key, None)
    with contextlib.suppress(Exception):
        cache_dir = _cache_dir(resolved)
        if cache_dir.exists():
            for f in cache_dir.glob("*.json"):
                with contextlib.suppress(Exception):
                    f.unlink(missing_ok=True)


def nearest_answer(
    project_path: str,
    scope: str,
    query: str,
    threshold: float = 0.86,
) -> dict[str, Any] | None:
    """Return the nearest precomputed answer whose embedding scores above threshold.

    Embeds the query via the GPU-locked embed_query call and cosine-matches
    against all stored embeddings for (project, scope).  Returns the best-
    scoring entry or None if nothing exceeds the threshold.
    """
    sig = _graph_sig(project_path)
    cache_dir = _cache_dir(project_path)
    if not cache_dir.exists():
        return None

    candidates: list[dict] = []
    prefix = f"{scope}__"
    for f in cache_dir.glob(f"{prefix}*.json"):
        with contextlib.suppress(Exception):
            entry = json.loads(f.read_text(encoding="utf-8"))
            if entry.get("_graph_sig") != sig:
                continue
            if "_embedding" not in entry:
                continue
            candidates.append(entry)

    if not candidates:
        return None

    # Embed the incoming query (GPU-locked, same path as search pipeline)
    try:
        from opencode_search.config import DEFAULT_DIMS, DEFAULT_EMBED_MODEL
        from opencode_search.embeddings import embed_query
        q_vec = embed_query(query, model=DEFAULT_EMBED_MODEL, dimensions=DEFAULT_DIMS)
    except Exception as exc:
        log.debug("answer_cache: embed_query failed: %s", exc)
        return None

    best_score = -1.0
    best_entry: dict[str, Any] | None = None
    for entry in candidates:
        score = _cosine(q_vec, entry["_embedding"])
        if score > best_score:
            best_score = score
            best_entry = entry

    if best_score >= threshold and best_entry is not None:
        log.debug(
            "answer_cache: nearest hit score=%.3f for '%s'", best_score, query[:60]
        )
        return best_entry
    return None
