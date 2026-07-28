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


def test_sd6_an_unreadable_file_is_not_reported_forever(safe_tmp_path, embedder):
    """SD6: a discoverable file that cannot be read must not be requeued every pass.

    `index_files` purges an unreadable path and returns without stamping a hash — it has no
    content to hash — so without a screen the drift trigger reports it, hands it over, and reports
    it again next pass, forever. SD3's zero-chunk case does not cover this: that file is readable
    and *does* get stamped.

    The readable half of the assertion is not decoration. A screen that reported nothing at all
    would satisfy the first assertion perfectly, so the gate has to show the trigger still sees
    the file it is supposed to see.
    """
    _seed(safe_tmp_path, embedder)
    blocked = safe_tmp_path / "blocked.py"
    blocked.write_text("def blocked():\n    return 1\n")
    (safe_tmp_path / "fine.py").write_text("def fine():\n    return 2\n")
    blocked.chmod(0o000)
    try:
        sweeps._code_fingerprint_cache.pop(str(safe_tmp_path), None)
        unindexed, _ = sweeps._index_set_drift(str(safe_tmp_path))
    finally:
        blocked.chmod(0o644)
    names = sorted(p.name for p in unindexed)
    assert "blocked.py" not in names, (
        "an unreadable file is reported unindexed; index_files cannot stamp it, so this repeats "
        "on every reconcile pass forever"
    )
    assert names == ["fine.py"], f"the screen swallowed a readable file too: {names}"


def test_sd5_chunks_without_a_hash_row_are_still_purgeable(safe_tmp_path, embedder):
    """SD5: the orphan side reads `file_hashes` UNION `chunks`, not `file_hashes` alone.

    A chunk row can outlive its hash row — 2,091 of inosoft's 5,242 indexed paths had no hash row
    at all, written by an index generation pre-dating the table. What keeps retrieving is the
    chunk, so keying orphans on `file_hashes` would leave every one of those unpurgeable the
    moment discovery stopped yielding it. Dropping the hash row here reproduces that generation
    exactly, against a real indexed store.
    """
    from rag_search.core.config import project_vector_db
    from rag_search.index.store import VectorStore

    _seed(safe_tmp_path, embedder)
    vs = VectorStore(project_vector_db(str(safe_tmp_path)), migrate=False)
    try:
        vs._con.execute("DELETE FROM file_hashes WHERE path LIKE '%b.py'")
        vs._con.commit()
    finally:
        vs.close()
    (safe_tmp_path / "b.py").unlink()
    sweeps._code_fingerprint_cache.pop(str(safe_tmp_path), None)

    _, orphaned = sweeps._index_set_drift(str(safe_tmp_path))
    assert [os.path.basename(p) for p in orphaned] == ["b.py"], (
        f"a path with chunks but no hash row was not reported orphaned: {orphaned}"
    )
    sweeps._purge_paths(str(safe_tmp_path), orphaned)
    sweeps._code_fingerprint_cache.pop(str(safe_tmp_path), None)
    _, orphaned = sweeps._index_set_drift(str(safe_tmp_path))
    assert not orphaned, f"purge left drift behind: {orphaned}"


def test_sd7_a_store_with_chunks_but_no_hash_table_is_repaired(safe_tmp_path, embedder):
    """SD7: an empty `file_hashes` alongside a full `chunks` is drift, not an unindexed project.

    The first cut of `_index_set_drift` bailed on `if not known`, reasoning that a project with no
    hash rows had never been indexed and was `_needs_index`'s business. `_needs_index` returns
    False for these — the store has chunks and communities and looks healthy — so nothing repaired
    them at all. Measured on the live fleet: **14 stores, 16,148 paths, 196,706 chunks** written by
    an index generation pre-dating the table, frozen across four consecutive reconcile passes.

    Same family as SD5, which fixed the orphan side of the identical asymmetry; this is the
    unindexed side.
    """
    from rag_search.core.config import project_vector_db
    from rag_search.index.store import VectorStore

    _seed(safe_tmp_path, embedder)
    vs = VectorStore(project_vector_db(str(safe_tmp_path)), migrate=False)
    try:
        vs._con.execute("DELETE FROM file_hashes")
        vs._con.commit()
        assert vs._con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], (
            "the store must still hold chunks, or this reproduces an empty store instead"
        )
    finally:
        vs.close()
    sweeps._code_fingerprint_cache.pop(str(safe_tmp_path), None)

    unindexed, _ = sweeps._index_set_drift(str(safe_tmp_path))
    assert sorted(p.name for p in unindexed) == ["a.py", "b.py"], (
        f"a store whose hash table is empty was skipped rather than repaired: {unindexed} — "
        "no other trigger covers it, so it stays stale forever"
    )


def test_sd7b_a_genuinely_empty_store_is_left_to_needs_index(safe_tmp_path):
    """SD7b: SD7's fix must not turn every never-indexed project into 100% drift.

    Without this, SD7 would pass under a `_index_set_drift` that had simply deleted the guard —
    and a project awaiting its first index would be repaired file-by-file through `_index_files`
    instead of by the full `index_project` that also builds its graph and communities.
    """
    from rag_search.core.config import project_vector_db
    from rag_search.index.store import VectorStore

    (safe_tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    VectorStore(project_vector_db(str(safe_tmp_path))).close()  # exists, holds nothing
    sweeps._code_fingerprint_cache.pop(str(safe_tmp_path), None)

    unindexed, orphaned = sweeps._index_set_drift(str(safe_tmp_path))
    assert not unindexed and not orphaned, (
        f"an empty store was reported as drift: {unindexed} — a first index is _needs_index's "
        "job, and routing it here skips graph and community construction"
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
