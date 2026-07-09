"""Live: bloat-gated VACUUM in maintenance() (Fix 1 — storage reclaim).

No mocks. No GPU required. Tests _vacuum_if_bloated + maintenance() integration.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from rag_search.daemon.sweeps import _VACUUM_BLOAT_BYTES, _vacuum_if_bloated

pytestmark = pytest.mark.live

_THRESHOLD = _VACUUM_BLOAT_BYTES  # 256 MB default


def _bloated_db(path: Path) -> Path:
    """Create a tiny SQLite DB that looks bloated (freelist >> threshold pages)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use a large page_size so few insertions produce freelist > threshold.
    # 65536 B pages × 5000 pages = 327 MB bloat (just above 256 MB threshold).
    page_count = 5000
    with sqlite3.connect(str(path)) as con:
        con.execute(f"PRAGMA page_size={65536}")
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("CREATE TABLE t (x TEXT)")
        con.executemany("INSERT INTO t VALUES (?)", [("a" * 50000,)] * page_count)
        con.commit()
        con.execute("DELETE FROM t")
        con.commit()
    # freelist_count now equals roughly the inserted pages → bloat > threshold
    with sqlite3.connect(str(path)) as con:
        fl = con.execute("PRAGMA freelist_count").fetchone()[0]
        ps = con.execute("PRAGMA page_size").fetchone()[0]
    assert fl * ps > _THRESHOLD, f"test setup failed: bloat {fl*ps//1024//1024}MB not > threshold"
    return path


def test_vacuum_reclaims_bloated_db(tmp_path):
    """_vacuum_if_bloated returns True and freelist drops to near zero."""
    db = _bloated_db(tmp_path / "bloated.db")
    result = _vacuum_if_bloated(db)
    assert result is True
    with sqlite3.connect(str(db)) as con:
        fl = con.execute("PRAGMA freelist_count").fetchone()[0]
        ps = con.execute("PRAGMA page_size").fetchone()[0]
    assert fl * ps <= _THRESHOLD, f"freelist still {fl*ps//1024//1024}MB after VACUUM"


def test_vacuum_skips_non_bloated_db(tmp_path):
    """_vacuum_if_bloated returns False when freelist is below threshold (no wasted VACUUM)."""
    db = tmp_path / "tiny.db"
    with sqlite3.connect(str(db)) as con:
        con.execute("CREATE TABLE t (x INTEGER)")
        con.execute("INSERT INTO t VALUES (1)")
        con.commit()
    result = _vacuum_if_bloated(db)
    assert result is False


def test_vacuum_handles_missing_file(tmp_path):
    """_vacuum_if_bloated is a no-op (False) when the file does not exist."""
    result = _vacuum_if_bloated(tmp_path / "nonexistent.db")
    assert result is False


def test_maintenance_vacuums_bloated_project_db():
    """maintenance() VACUUMs a registered project's DB when it is bloated."""
    from rag_search.core.config import ProjectEntry, project_vector_db
    from rag_search.core.registry import remove_project, upsert_project
    from rag_search.daemon.sweeps import maintenance

    safe_base = Path.home() / ".local" / "share" / "rse-test-dirs"
    safe_base.mkdir(parents=True, exist_ok=True)
    proj_dir = Path(tempfile.mkdtemp(dir=safe_base))
    try:
        upsert_project(ProjectEntry(path=str(proj_dir), enabled=True))
        vdb = project_vector_db(str(proj_dir))
        _bloated_db(vdb)
        maintenance()
        with sqlite3.connect(str(vdb)) as con:
            fl = con.execute("PRAGMA freelist_count").fetchone()[0]
            ps = con.execute("PRAGMA page_size").fetchone()[0]
        assert fl * ps <= _THRESHOLD, f"freelist still bloated: {fl*ps//1024//1024}MB"
    finally:
        remove_project(str(proj_dir))
        shutil.rmtree(proj_dir, ignore_errors=True)


def test_standalone_vectors_db_not_bloated():
    """Regression: standalone project vectors.db freelist must stay below threshold after reclaim."""
    from rag_search.core.config import project_vector_db
    from tests.live._projects import standalone_project

    entry_path = standalone_project()
    vdb = project_vector_db(entry_path)
    if not vdb.exists():
        return
    with sqlite3.connect(str(vdb)) as con:
        fl = con.execute("PRAGMA freelist_count").fetchone()[0]
        ps = con.execute("PRAGMA page_size").fetchone()[0]
    bloat_mb = fl * ps // (1024 * 1024)
    assert fl * ps <= _THRESHOLD, (
        f"standalone project vectors.db still bloated: {bloat_mb}MB freelist"
    )
