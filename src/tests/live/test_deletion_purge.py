"""Deletion-purge regression guard: a deleted file must leave the index.

When a watched source file is deleted, its chunks must vanish from the vector
store and from `search` — otherwise stale chunks for a file that no longer
exists linger, and a recall/graph query can still "hit" a gone file. (That is
exactly the failure the consumer-side golden-set integrity guard in
claude-code-workflows defends against; this locks it in from the engine side.)

The incremental reindex path already does the right thing: `index_files`
deletes every changed path's chunks up front, then skips re-embedding a file it
can no longer read (`OSError` → `continue`), so a deleted file is dropped rather
than re-added. The daemon's `sweeps._index_files` wraps that same call. These
tests pin that behavior so a future refactor can't silently reintroduce stale
deleted-file chunks.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def _paths_in_store(vs) -> set[str]:
    return {r[0] for r in vs._con.execute("SELECT DISTINCT path FROM chunks").fetchall()}


def test_index_files_purges_a_deleted_file(safe_tmp_path, embedder):
    """index_files() drops a file's chunks once the file is gone from disk,
    leaving unrelated files untouched."""
    from rag_search.index.indexer import index_files
    from rag_search.index.store import VectorStore

    root = safe_tmp_path / "proj"
    root.mkdir()
    keep = root / "keep.py"
    doomed = root / "doomed.py"
    keep.write_text("def keep_fn():\n    return 'kept'\n" * 5)
    doomed.write_text("def doomed_fn():\n    return 'gone soon'\n" * 5)

    vs = VectorStore(safe_tmp_path / "v.db")
    try:
        index_files([keep, doomed], embedder, vs, project_root=root)
        paths = _paths_in_store(vs)
        assert any("doomed.py" in p for p in paths), "doomed.py must be indexed first"
        assert any("keep.py" in p for p in paths)

        # Delete the file on disk, then re-run the incremental path for it.
        doomed.unlink()
        index_files([doomed], embedder, vs, project_root=root)

        paths_after = _paths_in_store(vs)
        assert not any("doomed.py" in p for p in paths_after), (
            "deleted file's chunks must be purged; still present: "
            f"{[p for p in paths_after if 'doomed' in p]}"
        )
        assert any("keep.py" in p for p in paths_after), "unrelated file's chunks must survive"
    finally:
        vs.close()


def test_deleted_file_disappears_from_search(safe_tmp_path, embedder):
    """After deletion + incremental reindex, `search` never returns the gone file."""
    from rag_search.index.indexer import index_files
    from rag_search.index.store import VectorStore
    from rag_search.query.search import search

    root = safe_tmp_path / "proj2"
    root.mkdir()
    a = root / "alpha.py"
    b = root / "beta.py"
    a.write_text("def alpha_widget_handler():\n    return 'alpha widget'\n" * 5)
    b.write_text("def beta_gadget_handler():\n    return 'beta gadget'\n" * 5)

    vs = VectorStore(safe_tmp_path / "v2.db")
    try:
        index_files([a, b], embedder, vs, project_root=root)
        hits = search("alpha widget handler", embedder, vs, scope="code", top_k=10)
        assert any("alpha.py" in r.get("path", "") for r in hits), "alpha.py must be findable first"

        a.unlink()
        index_files([a], embedder, vs, project_root=root)

        hits_after = search("alpha widget handler", embedder, vs, scope="code", top_k=10)
        assert not any("alpha.py" in r.get("path", "") for r in hits_after), (
            f"deleted file must not appear in search; got {[r.get('path') for r in hits_after]}"
        )
    finally:
        vs.close()


def test_daemon_incremental_reindex_purges_deleted_file(safe_tmp_path, embedder):
    """sweeps._index_files (the watcher's incremental entry) also purges a
    deleted file from the per-project store."""
    from rag_search.core.config import ProjectEntry, project_vector_db
    from rag_search.core.registry import remove_project, upsert_project
    from rag_search.daemon.sweeps import _index_files
    from rag_search.index.store import VectorStore

    root = safe_tmp_path / "proj3"
    root.mkdir()
    keep = root / "keep.py"
    doomed = root / "doomed.py"
    keep.write_text("def keep_fn():\n    return 1\n" * 5)
    doomed.write_text("def doomed_fn():\n    return 2\n" * 5)

    upsert_project(ProjectEntry(path=str(root), enabled=True))
    try:
        _index_files(str(root), [str(keep), str(doomed)])
        vdb = project_vector_db(str(root))
        vs = VectorStore(vdb)
        try:
            assert any("doomed.py" in p for p in _paths_in_store(vs)), "doomed.py must index first"
        finally:
            vs.close()

        doomed.unlink()
        _index_files(str(root), [str(doomed)])

        vs = VectorStore(vdb)
        try:
            paths_after = _paths_in_store(vs)
        finally:
            vs.close()
        assert not any("doomed.py" in p for p in paths_after), (
            "daemon incremental reindex must purge the deleted file: "
            f"{[p for p in paths_after if 'doomed' in p]}"
        )
        assert any("keep.py" in p for p in paths_after)
    finally:
        remove_project(str(root))
