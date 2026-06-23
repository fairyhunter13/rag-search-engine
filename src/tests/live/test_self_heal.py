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


def test_meta_round_trip(tmp_path):
    """T1a: get_meta/set_meta persist across close/reopen."""
    from opencode_search.core.config import project_graph_db
    from opencode_search.graph.store import GraphStore
    db = project_graph_db(str(tmp_path))
    gs = GraphStore(db)
    gs.set_meta("x", "hello")
    gs.commit()
    gs.close()
    gs2 = GraphStore(db)
    assert gs2.get_meta("x") == "hello"
    assert gs2.get_meta("absent") is None
    gs2.close()


def test_meta_migration_on_existing_db(tmp_path):
    """T1b: opening an old DB without the meta table triggers the schema migration."""
    from opencode_search.core.config import project_graph_db
    from opencode_search.graph.store import GraphStore
    db = project_graph_db(str(tmp_path))
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


def test_meta_survives_clear(tmp_path):
    """T1c: GraphStore.clear() wipes symbols/edges/communities but not meta."""
    from opencode_search.core.config import project_graph_db
    from opencode_search.graph.store import GraphStore
    db = project_graph_db(str(tmp_path))
    gs = GraphStore(db)
    gs.set_meta("version", "v1")
    gs.commit()
    gs.clear()
    assert gs.get_meta("version") == "v1", "meta must survive gs.clear()"
    gs.close()


def test_rederive_graph_has_no_embedder_call():
    """SG: _rederive_graph is GPU-free — must not call get_embedder or embed."""
    import inspect

    from opencode_search.daemon.sweeps import _rederive_graph
    src = inspect.getsource(_rederive_graph)
    assert "get_embedder" not in src, "_rederive_graph must not call get_embedder (GPU-free)"
    assert "embed(" not in src, "_rederive_graph must not call embed() (GPU-free)"


def test_pipeline_algo_version_reflects_both_constants():
    """T1d: _pipeline_algo_version() composes ALGO_VERSION + HIER_VERSION."""
    from opencode_search.daemon.sweeps import _pipeline_algo_version
    from opencode_search.graph.community import ALGO_VERSION
    from opencode_search.kb.hierarchy import HIER_VERSION
    assert _pipeline_algo_version() == f"{ALGO_VERSION}+{HIER_VERSION}"


def test_source_fingerprint_changes_on_file_add(tmp_path):
    """T1e: _source_fingerprint changes when a new file is added."""
    from opencode_search.daemon.sweeps import _source_fingerprint
    (tmp_path / "a.py").write_text("def f(): pass\n")
    sig1 = _source_fingerprint(str(tmp_path))
    (tmp_path / "b.py").write_text("def g(): pass\n")
    sig2 = _source_fingerprint(str(tmp_path))
    assert sig1 != sig2, "fingerprint must change when a file is added"


def test_graph_stale_fires_on_poisoned_version(tmp_path):
    """T1f: _graph_stale returns True when meta[algo_version] is wrong."""
    from opencode_search.core.config import project_graph_db
    from opencode_search.daemon.sweeps import (
        _graph_stale,
        _pipeline_algo_version,
        _source_fingerprint,
    )
    from opencode_search.graph.store import GraphStore
    (tmp_path / "a.py").write_text("def f(): pass\n")
    db = project_graph_db(str(tmp_path))
    gs = GraphStore(db)
    gs.set_meta("algo_version", _pipeline_algo_version())
    gs.set_meta("source_sig", _source_fingerprint(str(tmp_path)))
    gs.commit()
    assert not _graph_stale(str(tmp_path), gs), "up-to-date stamps must not be stale"
    gs.set_meta("algo_version", "STALE")
    gs.commit()
    assert _graph_stale(str(tmp_path), gs), "poisoned version must be stale"
    gs.close()
