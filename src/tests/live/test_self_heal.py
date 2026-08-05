"""Self-healing pipeline — fast tests: meta stamps + source-guard.

T1a — get_meta/set_meta persist across close/reopen
T1b — old DB without meta table gets migrated on open
T1c — GraphStore.clear() preserves meta rows
SG  — _rederive_graph has no GPU calls
"""
from __future__ import annotations

import sqlite3

import pytest

pytestmark = pytest.mark.live


def test_meta_round_trip(safe_tmp_path):
    """T1a: get_meta/set_meta persist across close/reopen."""
    from rag_search.core.config import project_graph_db
    from rag_search.graph.store import GraphStore
    db = project_graph_db(str(safe_tmp_path))
    gs = GraphStore(db)
    gs.set_meta("x", "hello")
    gs.commit()
    gs.close()
    gs2 = GraphStore(db)
    assert gs2.get_meta("x") == "hello"
    assert gs2.get_meta("absent") is None
    gs2.close()


def test_meta_migration_on_existing_db(safe_tmp_path):
    """T1b: opening an old DB without the meta table triggers the schema migration."""
    from rag_search.core.config import project_graph_db
    from rag_search.graph.store import GraphStore
    db = project_graph_db(str(safe_tmp_path))
    # Create a fully valid DB first, then drop meta to simulate a pre-M1 DB.
    gs = GraphStore(db)
    gs.close()
    with sqlite3.connect(str(db)) as con:
        con.execute("DROP TABLE IF EXISTS meta")
        con.commit()
    # Re-open: _open() schema migration must recreate meta.
    gs2 = GraphStore(db)
    gs2.set_meta("migrated", "1")
    gs2.commit()
    assert gs2.get_meta("migrated") == "1"
    gs2.close()


def test_meta_survives_clear(safe_tmp_path):
    """T1c: GraphStore.clear() wipes symbols/edges/communities but not meta."""
    from rag_search.core.config import project_graph_db
    from rag_search.graph.store import GraphStore
    db = project_graph_db(str(safe_tmp_path))
    gs = GraphStore(db)
    gs.set_meta("version", "v1")
    gs.commit()
    gs.clear()
    assert gs.get_meta("version") == "v1", "meta must survive gs.clear()"
    gs.close()


def test_rederive_graph_has_no_embedder_call():
    """SG: _rederive_graph is GPU-free — must not call get_embedder or embed."""
    import inspect

    from rag_search.daemon.sweeps import _rederive_graph
    src = inspect.getsource(_rederive_graph)
    assert "get_embedder" not in src, "_rederive_graph must not call get_embedder (GPU-free)"
    assert "embed(" not in src, "_rederive_graph must not call embed() (GPU-free)"


def test_pipeline_algo_version_reflects_all_three_constants():
    """T1d: _pipeline_algo_version() composes ALGO_VERSION + EXTRACTOR_REV + code_fp.

    EXTRACTOR_REV joined the identity with S3, because resolution lives in
    `sweeps._extract_graph` and no byte fingerprint covers it — see TS3.
    """
    from rag_search.daemon.sweeps import _code_fingerprint, _pipeline_algo_version
    from rag_search.graph.community import ALGO_VERSION
    from rag_search.graph.extractor import EXTRACTOR_REV
    expected = f"{ALGO_VERSION}+{EXTRACTOR_REV}+{_code_fingerprint()}"
    assert _pipeline_algo_version() == expected


def test_source_fingerprint_changes_on_file_add(tmp_path):
    """T1e: the drift gate's fingerprint changes when a new code file is added.

    Repointed 2026-08-05 from the orphaned `_source_fingerprint` to `_code_source_fingerprint`.
    Its memo is TTL-keyed, so it must be dropped between the two calls — otherwise this asserts
    the memo's patience rather than the walk's honesty, and would pass on a gate that had gone
    completely blind.
    """
    from rag_search.daemon import sweeps
    (tmp_path / "a.py").write_text("def f(): pass\n")
    sweeps._code_fingerprint_cache.pop(str(tmp_path), None)
    sig1 = sweeps._code_source_fingerprint(str(tmp_path))
    (tmp_path / "b.py").write_text("def g(): pass\n")
    sweeps._code_fingerprint_cache.pop(str(tmp_path), None)
    sig2 = sweeps._code_source_fingerprint(str(tmp_path))
    assert sig1 != sig2, "fingerprint must change when a file is added"


def test_graph_stale_fires_on_poisoned_version(safe_tmp_path):
    """T1f: _graph_stale returns True when meta[algo_version] is wrong."""
    from rag_search.core.config import project_graph_db
    from rag_search.daemon.sweeps import (
        _code_source_fingerprint,
        _graph_stale,
        _pipeline_algo_version,
    )
    from rag_search.graph.store import GraphStore
    (safe_tmp_path / "a.py").write_text("def f(): pass\n")
    db = project_graph_db(str(safe_tmp_path))
    gs = GraphStore(db)
    gs.set_meta("algo_version", _pipeline_algo_version())
    gs.set_meta("source_sig", _code_source_fingerprint(str(safe_tmp_path)))
    gs.commit()
    assert not _graph_stale(str(safe_tmp_path), gs), "up-to-date stamps must not be stale"
    gs.set_meta("algo_version", "STALE")
    gs.commit()
    assert _graph_stale(str(safe_tmp_path), gs), "poisoned version must be stale"
    gs.close()
