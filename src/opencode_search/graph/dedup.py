"""Entity deduplication for the code graph.

Finds duplicate nodes (same symbol indexed multiple times with slight name variations)
and merges them, redirecting edges to the canonical node.

Pipeline (graphify-compatible):
  1. Exact normalization     — strip whitespace, lowercase key fields
  2. Entropy gate            — skip nodes with trivially short/generic names
  3. MinHash/LSH blocking    — O(n) candidate-pair generation (requires datasketch)
  4. Jaro-Winkler verification — confirm similarity score ≥ threshold
  5. Same-community boost    — nodes in the same community get a +0.05 score bonus
  6. Union-find merge        — pick canonical node, redirect edges, delete duplicates

Fallback: when datasketch or rapidfuzz are unavailable, uses exact-norm-only dedup
(same qualified_name + same file → merge, keeping most-recent updated_at).
"""
from __future__ import annotations

import contextlib
import importlib.util as _iutil
import logging
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .storage import GraphStorage, NodeData

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature detection — graceful fallback if extras not installed
# ---------------------------------------------------------------------------

_HAVE_DATASKETCH = _iutil.find_spec("datasketch") is not None

try:
    from rapidfuzz.distance import JaroWinkler as _JaroWinkler
    _HAVE_RAPIDFUZZ = True
except ImportError:
    _JaroWinkler = None  # type: ignore[assignment]
    _HAVE_RAPIDFUZZ = False

_FUZZY_AVAILABLE = _HAVE_DATASKETCH and _HAVE_RAPIDFUZZ


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class DedupResult:
    merged_pairs: list[tuple[str, str]] = field(default_factory=list)
    merged_count: int = 0
    candidate_pairs_checked: int = 0
    strategy: str = "exact"
    skipped_low_entropy: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

# Information threshold for the entropy gate: H(t) * len(t) must exceed this
# value for a symbol name to be considered worth deduplicating.
# Tuned so that short/common words ("teardown" max score: 8 chars × 3.0 bits = 24.0)
# fall below it while compound names score well above (e.g. "process_order"=37.3,
# "PaymentGateway"=44.8).  Derived from character-level information, not a keyword list.
_INFO_THRESHOLD: float = 25.0


def _info_score(text: str) -> float:
    """Shannon entropy × length (character-level information content)."""
    if not text:
        return 0.0
    freq: dict[str, int] = {}
    for ch in text:
        freq[ch] = freq.get(ch, 0) + 1
    n = len(text)
    return -n * sum((c / n) * math.log2(c / n) for c in freq.values())


def _norm(text: str) -> str:
    """Normalize a symbol name: lowercase, strip, collapse whitespace."""
    return " ".join(text.strip().lower().split())


def _entropy(text: str, min_len: int = 4) -> bool:
    """Return True iff the text has enough information to be worth deduping.

    Derived from character-level information content (Shannon entropy × length)
    compared against _INFO_THRESHOLD — not a fixed keyword denylist.
    """
    t = _norm(text)
    if len(t) < min_len:
        return False
    if t.isdigit():
        return False
    return _info_score(t) >= _INFO_THRESHOLD


def _shingles(text: str, k: int = 3) -> set[str]:
    """Character k-shingles for MinHash input."""
    t = _norm(text)
    if len(t) < k:
        return {t}
    return {t[i : i + k] for i in range(len(t) - k + 1)}


def _make_minhash(text: str, num_perm: int = 128):  # type: ignore[return]
    from datasketch import MinHash
    m = MinHash(num_perm=num_perm)
    for shingle in _shingles(text):
        m.update(shingle.encode("utf8"))
    return m


def _jaro_winkler(a: str, b: str) -> float:
    """Jaro-Winkler similarity in [0, 1]."""
    if _HAVE_RAPIDFUZZ and _JaroWinkler is not None:
        return _JaroWinkler.normalized_similarity(a, b)
    return 1.0 if _norm(a) == _norm(b) else 0.0


# ---------------------------------------------------------------------------
# Union-Find
# ---------------------------------------------------------------------------

class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        if x not in self._parent:
            self._parent[x] = x
        if self._parent[x] != x:
            self._parent[x] = self.find(self._parent[x])
        return self._parent[x]

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


# ---------------------------------------------------------------------------
# Candidate key for similarity comparison
# ---------------------------------------------------------------------------

def _candidate_key(node: NodeData) -> str:
    """Composite key used for dedup similarity: qualified_name + kind."""
    return f"{node.qualified_name}:{node.kind}"


# ---------------------------------------------------------------------------
# Main dedup entry point
# ---------------------------------------------------------------------------

def dedup_nodes(
    storage: GraphStorage,
    *,
    threshold: float = 0.88,
    num_perm: int = 128,
    lsh_threshold: float = 0.75,
    community_boost: float = 0.05,
    dry_run: bool = False,
) -> DedupResult:
    """Find and merge duplicate nodes in the graph.

    Two nodes are considered duplicates when they have the same kind and file,
    and their qualified names are sufficiently similar (Jaro-Winkler ≥ threshold).

    In fuzzy mode (datasketch + rapidfuzz available), MinHash/LSH generates
    candidate pairs efficiently; Jaro-Winkler confirms.

    In exact mode (fallback), only nodes sharing the same (file, kind, norm_name)
    triple are merged.

    Args:
        storage:          Open GraphStorage instance.
        threshold:        Jaro-Winkler similarity required for merge (0.88 default).
        num_perm:         MinHash permutation count (higher = more accurate, slower).
        lsh_threshold:    LSH Jaccard threshold for candidate generation.
        community_boost:  Extra score added when both nodes share the same community.
        dry_run:          If True, report pairs but do not modify the graph.

    Returns:
        DedupResult with stats.
    """
    result = DedupResult(strategy="fuzzy" if _FUZZY_AVAILABLE else "exact")

    nodes = storage.all_nodes()
    if len(nodes) < 2:
        return result

    by_file_kind: dict[tuple[str, str], list[NodeData]] = defaultdict(list)
    for n in nodes:
        if _entropy(n.name):
            by_file_kind[(n.file, n.kind)].append(n)
        else:
            result.skipped_low_entropy += 1

    uf = _UnionFind()

    if _FUZZY_AVAILABLE:
        pairs_checked = _find_pairs_fuzzy(
            by_file_kind, uf, threshold, num_perm, lsh_threshold, community_boost,
        )
    else:
        pairs_checked = _find_pairs_exact(by_file_kind, uf)

    result.candidate_pairs_checked = pairs_checked

    if dry_run:
        _count_merges(nodes, uf, result)
        return result

    _apply_merges(storage, nodes, uf, result)
    return result


def _find_pairs_fuzzy(
    by_file_kind: dict[tuple[str, str], list[NodeData]],
    uf: _UnionFind,
    threshold: float,
    num_perm: int,
    lsh_threshold: float,
    community_boost: float,
) -> int:
    """Fuzzy pair finding: MinHash/LSH blocking + Jaro-Winkler confirm."""
    from datasketch import MinHashLSH

    pairs_checked = 0

    for group in by_file_kind.values():
        if len(group) < 2:
            continue

        lsh = MinHashLSH(threshold=lsh_threshold, num_perm=num_perm)
        minhashes: dict[str, object] = {}

        for n in group:
            key = _candidate_key(n)
            m = _make_minhash(key, num_perm=num_perm)
            minhashes[n.id] = m
            with contextlib.suppress(ValueError):
                lsh.insert(n.id, m)

        for n in group:
            try:
                candidates = lsh.query(minhashes[n.id])
            except Exception:
                continue
            for cid in candidates:
                if cid == n.id:
                    continue
                pairs_checked += 1
                other = next((x for x in group if x.id == cid), None)
                if other is None:
                    continue
                score = _jaro_winkler(_norm(n.qualified_name), _norm(other.qualified_name))
                if (
                    n.community_id is not None
                    and n.community_id == other.community_id
                ):
                    score = min(1.0, score + community_boost)
                if score >= threshold:
                    canonical, duplicate = _pick_canonical(n, other)
                    uf.union(canonical.id, duplicate.id)

    return pairs_checked


def _find_pairs_exact(
    by_file_kind: dict[tuple[str, str], list[NodeData]],
    uf: _UnionFind,
) -> int:
    """Exact-norm fallback: group by normalized qualified_name."""
    pairs_checked = 0

    for group in by_file_kind.values():
        if len(group) < 2:
            continue
        by_norm: dict[str, list[NodeData]] = defaultdict(list)
        for n in group:
            by_norm[_norm(n.qualified_name)].append(n)
        for same_name_nodes in by_norm.values():
            if len(same_name_nodes) < 2:
                continue
            same_name_nodes.sort(key=lambda n: n.updated_at or "", reverse=True)
            canonical = same_name_nodes[0]
            for dup in same_name_nodes[1:]:
                pairs_checked += 1
                uf.union(canonical.id, dup.id)

    return pairs_checked


def _pick_canonical(a: NodeData, b: NodeData) -> tuple[NodeData, NodeData]:
    """Return (canonical, duplicate) — keep the node with the more recent updated_at."""
    if (a.updated_at or "") >= (b.updated_at or ""):
        return a, b
    return b, a


def _count_merges(nodes: list[NodeData], uf: _UnionFind, result: DedupResult) -> None:
    """Populate result.merged_pairs without modifying the DB."""
    for n in nodes:
        root = uf.find(n.id)
        if root == n.id:
            continue
        result.merged_pairs.append((root, n.id))
        result.merged_count += 1


def _apply_merges(
    storage: GraphStorage,
    nodes: list[NodeData],
    uf: _UnionFind,
    result: DedupResult,
) -> None:
    """Redirect edges to canonical nodes and delete duplicate nodes."""
    canonical_map: dict[str, str] = {}

    for n in nodes:
        root = uf.find(n.id)
        if root != n.id:
            canonical_map[n.id] = root

    if not canonical_map:
        return

    db = storage._db()
    try:
        with db:
            for dup_id, canon_id in canonical_map.items():
                # OR IGNORE: if the redirected edge would collide with an existing one,
                # silently drop it — the existing edge already covers the relationship.
                # Remaining un-redirected rows get cleaned by FK CASCADE on node delete.
                db.execute(
                    "UPDATE OR IGNORE edges SET from_id=? WHERE from_id=?",
                    (canon_id, dup_id),
                )
                db.execute(
                    "UPDATE OR IGNORE edges SET to_id=? WHERE to_id=?",
                    (canon_id, dup_id),
                )
                # Remove self-loops
                db.execute(
                    "DELETE FROM edges WHERE from_id=? AND to_id=?",
                    (canon_id, canon_id),
                )

            for dup_id, canon_id in canonical_map.items():
                db.execute("DELETE FROM nodes WHERE id=?", (dup_id,))
                result.merged_pairs.append((canon_id, dup_id))
                result.merged_count += 1

    except sqlite3.Error as exc:
        result.errors.append(f"DB error during merge: {exc}")
        log.warning("dedup merge error: %s", exc)
