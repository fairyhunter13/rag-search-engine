"""Self-healing pipeline — slow e2e tests (require GPU + DeepSeek key).

T2 — algorithm-version drift triggers reconcile to re-derive the graph
T3 — source-fingerprint drift triggers reconcile to re-extract new symbols
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.live


@pytest.fixture()
def _proj(tmp_path):
    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.registry import remove_project, upsert_project
    proj = str(tmp_path)
    upsert_project(ProjectEntry(path=proj, enabled=True))
    yield proj
    remove_project(proj)


@pytest.mark.slow
def test_algo_drift_triggers_rederive(_proj):
    """T2: poisoning meta[algo_version] causes reconcile to re-derive the graph."""
    from opencode_search.core.config import project_graph_db
    from opencode_search.daemon.sweeps import (
        _index_project,
        _pipeline_algo_version,
        reconcile_projects,
    )
    from opencode_search.graph.store import GraphStore

    proj = _proj
    p = Path(proj)
    (p / "a.py").write_text("def foo():\n    bar()\n\ndef bar():\n    pass\n")
    (p / "b.py").write_text("def baz():\n    foo()\n")
    _index_project(proj)

    db = project_graph_db(proj)
    gs = GraphStore(db)
    try:
        assert gs.get_meta("algo_version") == _pipeline_algo_version(), \
            "fresh index must stamp algo_version"
        gs.set_meta("algo_version", "STALE_ALGO_X")
        gs.commit()
    finally:
        gs.close()

    reconcile_projects()

    gs2 = GraphStore(db)
    try:
        assert gs2.get_meta("algo_version") == _pipeline_algo_version(), \
            "reconcile must restamp algo_version after re-derive"
        assert gs2.get_meta("source_sig") is not None, "reconcile must stamp source_sig"
        l1 = gs2._con.execute("SELECT COUNT(*) FROM communities WHERE level=1").fetchone()[0]
        assert l1 >= 1, "re-derive must produce at least 1 L1 community"
    finally:
        gs2.close()


@pytest.mark.slow
def test_source_drift_triggers_rederive(_proj):
    """T3: adding a new source file changes the fingerprint; reconcile re-extracts it."""
    from opencode_search.core.config import project_graph_db
    from opencode_search.daemon.sweeps import (
        _index_project,
        _source_fingerprint,
        reconcile_projects,
    )
    from opencode_search.graph.store import GraphStore

    proj = _proj
    p = Path(proj)
    (p / "seed.py").write_text("def seed_fn():\n    pass\n")
    _index_project(proj)

    db = project_graph_db(proj)
    gs = GraphStore(db)
    try:
        sig_before = gs.get_meta("source_sig")
        assert sig_before is not None, "fresh index must stamp source_sig"
        names = [r[0] for r in gs._con.execute("SELECT name FROM symbols").fetchall()]
        assert "seed_fn" in names, "seed_fn must be extracted after first index"
    finally:
        gs.close()

    (p / "new_module.py").write_text("def brand_new_fn():\n    pass\n")
    sig_new = _source_fingerprint(proj)
    assert sig_new != sig_before, "fingerprint must change after adding a file"

    reconcile_projects()

    gs2 = GraphStore(db)
    try:
        assert gs2.get_meta("source_sig") == sig_new, \
            "reconcile must rewrite source_sig to current value"
        names2 = [r[0] for r in gs2._con.execute("SELECT name FROM symbols").fetchall()]
        assert "brand_new_fn" in names2, \
            "brand_new_fn must appear in symbols after reconcile re-extracted the graph"
    finally:
        gs2.close()
