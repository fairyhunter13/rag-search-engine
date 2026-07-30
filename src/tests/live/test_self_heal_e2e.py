"""Self-healing pipeline — slow e2e tests (require GPU; there is no LLM in this lane).

T2 — algorithm-version drift triggers reconcile to re-derive the graph
T3 — source-fingerprint drift triggers reconcile to re-extract new symbols
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.live


@pytest.fixture()
def _no_code_scan_memo():
    """Set `_code_scan`'s TTL to zero for one test, restoring whatever was there before.

    Deliberately `os.environ` + `try/finally` rather than pytest's monkey-patching fixture,
    matching `test_federation_exclude.py`'s FE1-FE3. `test_no_mocks_or_fakes.py` bans that
    fixture by name, and the bluntness is worth keeping: narrowing the ban to its attribute-
    substituting methods would be defeated by binding the fixture to any other name, whereas
    `os.environ` cannot substitute a component at all — it can only set config, which is what
    this is. Setting a knob production reads is real integration, not a double. (That guard
    matches raw lines, so it sees prose too; naming the fixture here would trip it.)
    """
    key = "RSE_CODE_SCAN_TTL_S"
    orig = os.environ.get(key)
    os.environ[key] = "0"
    try:
        yield
    finally:
        if orig is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = orig


@pytest.fixture()
def _proj():
    import shutil

    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import remove_project, upsert_project
    from tests.live._projects import make_run_dir
    d = make_run_dir("self-heal-")
    try:
        upsert_project(ProjectEntry(path=str(d), enabled=True))
        yield str(d)
        remove_project(str(d))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_algo_drift_triggers_rederive(_proj):
    """T2: poisoning meta[algo_version] causes reconcile to re-derive the graph."""
    from rag_search.core.config import project_graph_db
    from rag_search.daemon.sweeps import (
        _index_project,
        _pipeline_algo_version,
        reconcile_projects,
    )
    from rag_search.graph.store import GraphStore

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


def test_source_drift_triggers_rederive(_proj, _no_code_scan_memo):
    """T3: adding a new source file changes the fingerprint; reconcile re-extracts it.

    `_code_scan` memoises on *elapsed time* (`_CODE_SCAN_TTL_S = 300`), so a fingerprint taken
    immediately after a write is served from the pre-write cache entry by construction — this
    test asserted the memo did not exist and had failed on every run since the memo landed,
    invisibly, because it *was* `@pytest.mark.slow` (that mark is gone since S1/S2,
    2026-07-30 — it cost wall clock, never a model). The memo is correct: it bounds how often a
    43 s fleet-wide stat-walk is paid, against a reconcile cadence measured in half-hours, and
    live edits are the watcher's job rather than reconcile's. So the fix is to opt out through
    the knob production already exposes for exactly this, not to weaken the assertion — the
    invariant under test is "a new file reaches the graph", never "no cache stands in front".
    It must cover `reconcile_projects()` too: with a warm pre-write entry reconcile compares
    stored sig against cached sig, finds them equal, and skips the re-extract this asserts.
    """
    from rag_search.core.config import project_graph_db
    from rag_search.daemon.sweeps import (
        _code_source_fingerprint,
        _index_project,
        reconcile_projects,
    )
    from rag_search.graph.store import GraphStore

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
    sig_new = _code_source_fingerprint(proj)
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
