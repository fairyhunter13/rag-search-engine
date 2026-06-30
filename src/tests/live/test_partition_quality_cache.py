"""PQC — partition-quality cache: write-side persistence + read-side sig validation (GPU-free).

PQC1 — _persist_partition_quality stores a 'partition_quality' meta entry in graph.db.
PQC2 — cached quality dict equals a fresh partition_quality() call on the same store.
PQC3 — after a content change (new symbol+edge), the stored sig no longer matches current
        counts, proving the cache self-invalidates and the read-side will fall back correctly.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.live


def _build_mini_store(db_path: Path):
    """Minimal GraphStore: 2 symbols, 1 cross-file edge, community-detected."""
    from opencode_search.graph.community import detect_communities
    from opencode_search.graph.store import GraphStore

    gs = GraphStore(db_path)
    gs.upsert_symbol("s1", "func_a", "pkg.func_a", "function", "pkg/a.py", 1, 5, "python")
    gs.upsert_symbol("s2", "func_b", "pkg.func_b", "function", "pkg/b.py", 1, 5, "python")
    gs.upsert_edge("s1", "s2")
    gs.commit()
    detect_communities(gs)
    return gs


def test_pqc1_persist_writes_meta(tmp_path):
    """_persist_partition_quality must write a non-null 'partition_quality' meta entry."""
    from opencode_search.daemon.sweeps import _persist_partition_quality

    gs = _build_mini_store(tmp_path / "graph.db")
    try:
        _persist_partition_quality(gs)
        gs.commit()
        raw = gs.get_meta("partition_quality")
    finally:
        gs.close()

    assert raw is not None, "_persist_partition_quality must store a meta entry"
    cached = json.loads(raw)
    assert "sig" in cached, f"meta must contain 'sig' key: {cached}"
    assert "q" in cached, f"meta must contain 'q' key: {cached}"


def test_pqc2_cached_equals_fresh(tmp_path):
    """Cached quality dict must be bit-identical to a fresh partition_quality() call."""
    from opencode_search.daemon.sweeps import _persist_partition_quality
    from opencode_search.graph.quality import partition_quality

    gs = _build_mini_store(tmp_path / "graph.db")
    try:
        _persist_partition_quality(gs)
        gs.commit()
        raw = gs.get_meta("partition_quality")
        cached = json.loads(raw)
        fresh_hq = partition_quality(gs)
        expected_sig = f"{gs.symbol_count()}:{gs.edge_count()}:{gs.community_count()}"
    finally:
        gs.close()

    assert cached["sig"] == expected_sig, (
        f"stored sig {cached['sig']!r} must match current counts {expected_sig!r}"
    )
    assert cached["q"] == fresh_hq, (
        f"cached quality must equal a fresh computation:\n  cached={cached['q']}\n  fresh={fresh_hq}"
    )


def test_pqc3_sig_invalidates_on_content_change(tmp_path):
    """After adding a symbol+edge, the stored sig must differ from current counts.

    This proves the cache self-invalidates: the read-side in overview(status) computes
    f'{s}:{ec}:{cm}' from live counts; if it does not match the stored sig it falls
    back to partition_quality(gs), which is the correct behaviour.
    """
    from opencode_search.daemon.sweeps import _persist_partition_quality

    gs = _build_mini_store(tmp_path / "graph.db")
    try:
        _persist_partition_quality(gs)
        gs.commit()
        cached_sig = json.loads(gs.get_meta("partition_quality"))["sig"]

        gs.upsert_symbol("s3", "func_c", "pkg.func_c", "function", "pkg/c.py", 1, 5, "python")
        gs.upsert_edge("s2", "s3")
        gs.commit()
        current_sig = f"{gs.symbol_count()}:{gs.edge_count()}:{gs.community_count()}"
    finally:
        gs.close()

    assert cached_sig != current_sig, (
        f"sig must differ after content change (cache must be invalid); "
        f"cached={cached_sig!r} current={current_sig!r}"
    )
