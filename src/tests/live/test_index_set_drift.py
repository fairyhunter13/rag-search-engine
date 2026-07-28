"""SD1-SD4: the index matches what discovery yields, not merely what has changed recently.

Every staleness check that existed before these was a *change* detector — "is anything newer
than my last pass". The index's contents are decided by discovery policy, so a lowered size cap
or a newly excluded suffix retroactively changes what should be indexed and moves no file's
mtime. Measured on the live fleet the day this landed, with all of those checks reporting
convergence: 42,952 chunks indexed that discovery rejects today, 378 discoverable files never
indexed, and one deleted probe file still searchable weeks later.
"""
import os
import time

import pytest

from rag_search.daemon import sweeps

pytestmark = pytest.mark.live


def _seed(root, embedder):
    """A real indexed project: two source files through the real indexer, no fakes."""
    from rag_search.core.config import project_vector_db
    from rag_search.index.indexer import index_project
    from rag_search.index.store import VectorStore

    (root / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "b.py").write_text("def beta():\n    return alpha()\n")
    vs = VectorStore(project_vector_db(str(root)))
    try:
        index_project(str(root), embedder, vs)
    finally:
        vs.close()


def test_sd1_discoverable_but_unindexed_file_is_reported(safe_tmp_path, embedder):
    """SD1: a file added without a watcher event shows up as unindexed."""
    _seed(safe_tmp_path, embedder)
    unindexed, orphaned = sweeps._index_set_drift(str(safe_tmp_path))
    assert not unindexed and not orphaned, f"freshly indexed project already drifts: {unindexed}"

    (safe_tmp_path / "c.py").write_text("def gamma():\n    return beta()\n")
    sweeps._code_fingerprint_cache.pop(str(safe_tmp_path), None)
    unindexed, orphaned = sweeps._index_set_drift(str(safe_tmp_path))
    assert [p.name for p in unindexed] == ["c.py"], f"expected c.py unindexed, got {unindexed}"
    assert not orphaned


def test_sd2_indexed_path_discovery_rejects_is_purged(safe_tmp_path, embedder):
    """SD2: a file the index holds but discovery no longer yields is reported and purged.

    Deleting the file is the cheap way to make discovery reject it; the fleet's real instance was
    a 169 kB YAML that still exists and merely exceeds `_SIZE_LIMITS['data']`. Both reach
    `_index_set_drift` by the same route — absent from the walk, present in `file_hashes`.
    """
    from rag_search.core.config import project_vector_db
    from rag_search.index.store import VectorStore

    _seed(safe_tmp_path, embedder)
    (safe_tmp_path / "b.py").unlink()
    sweeps._code_fingerprint_cache.pop(str(safe_tmp_path), None)

    unindexed, orphaned = sweeps._index_set_drift(str(safe_tmp_path))
    assert not unindexed
    assert [os.path.basename(p) for p in orphaned] == ["b.py"], orphaned

    vs = VectorStore(project_vector_db(str(safe_tmp_path)), migrate=False)
    try:
        before = vs._con.execute(
            "SELECT COUNT(*) FROM chunks WHERE path LIKE '%b.py'").fetchone()[0]
    finally:
        vs.close()
    assert before, "b.py must really be in the index, or the purge below proves nothing"

    sweeps._purge_paths(str(safe_tmp_path), orphaned)
    sweeps._code_fingerprint_cache.pop(str(safe_tmp_path), None)
    unindexed, orphaned = sweeps._index_set_drift(str(safe_tmp_path))
    assert not orphaned, f"purge left drift behind: {orphaned}"

    vs = VectorStore(project_vector_db(str(safe_tmp_path)), migrate=False)
    try:
        after = vs._con.execute(
            "SELECT COUNT(*) FROM chunks WHERE path LIKE '%b.py'").fetchone()[0]
    finally:
        vs.close()
    assert after == 0, f"{after} chunk(s) of a purged path still searchable"


def test_sd3_file_that_chunks_to_nothing_does_not_drift_forever(safe_tmp_path, embedder):
    """SD3: an empty file is processed-and-done, not eternally unindexed.

    This is why the comparison is against `file_hashes` and not `chunks`. An empty `__init__.py`
    is discoverable and yields no chunks; keyed on `chunks` it would be re-queued on every
    reconcile pass forever — 68 of the fleet's 446 unindexed files are exactly this shape, and
    `_graph_needs_full_index` had already been rescued from the same never-satisfiable gate.
    """
    _seed(safe_tmp_path, embedder)
    # One byte, not zero: discovery skips a genuinely empty file, so a 0-byte fixture would make
    # this test pass without ever exercising the case. The fleet's 68 real instances are exactly
    # this — a lone newline that is discoverable and chunks to nothing.
    (safe_tmp_path / "__init__.py").write_text("\n")
    sweeps._code_fingerprint_cache.pop(str(safe_tmp_path), None)

    unindexed, _ = sweeps._index_set_drift(str(safe_tmp_path))
    assert [p.name for p in unindexed] == ["__init__.py"]

    sweeps._index_files(str(safe_tmp_path), unindexed)
    sweeps._code_fingerprint_cache.pop(str(safe_tmp_path), None)
    unindexed, _ = sweeps._index_set_drift(str(safe_tmp_path))
    assert not unindexed, (
        f"a zero-chunk file is still reported unindexed after being processed: {unindexed} — "
        "the check is keyed on chunks rather than file_hashes and will loop forever"
    )


def test_sd4_scan_sees_a_change_nested_below_the_root(safe_tmp_path):
    """SD4: the walk's memo must not be keyed on the root directory's mtime.

    A directory's mtime moves only when its *direct* entries change, so keying the memo on it
    made every nested edit invisible — the fingerprint and the mtime watermark froze, and since
    the baseline they are compared against keeps advancing, staleness read "clean" permanently.
    Live at the time: rag-search-engine's key was 9.1 h older than its newest file.

    The control is the first assertion. Without it this test would pass for the wrong reason on
    any platform where a nested write happens to touch the root.
    """
    (safe_tmp_path / "pkg").mkdir()
    (safe_tmp_path / "pkg" / "a.py").write_text("def alpha():\n    return 1\n")

    old = os.environ.get("RSE_CODE_SCAN_TTL_S")
    os.environ["RSE_CODE_SCAN_TTL_S"] = "0.05"
    try:
        sweeps._code_fingerprint_cache.pop(str(safe_tmp_path), None)
        first, _ = sweeps._code_scan(str(safe_tmp_path))
        root_mtime = safe_tmp_path.stat().st_mtime

        time.sleep(0.1)
        (safe_tmp_path / "pkg" / "b.py").write_text("def beta():\n    return 2\n")
        assert safe_tmp_path.stat().st_mtime == root_mtime, (
            "nested write moved the root mtime on this platform — the defect this test guards "
            "cannot occur here, so the assertion below would prove nothing"
        )

        time.sleep(0.1)
        second, _ = sweeps._code_scan(str(safe_tmp_path))
    finally:
        if old is None:
            os.environ.pop("RSE_CODE_SCAN_TTL_S", None)
        else:
            os.environ["RSE_CODE_SCAN_TTL_S"] = old
    assert second != first, (
        "a file created below the project root did not change the code fingerprint — the scan "
        "memo is keyed on something that cannot observe nested changes"
    )
