"""VF1-VF4: freshness must be reported by the watermark, and verified by content.

Two halves of one defect, both found by a session that read `overview(what="status")` on a
current index, took `indexed_at` for its age, declared it 17 days stale, and fell back to grep.

The first half is reporting: `indexed_at` stamps the last *full* build and incremental passes
deliberately never restamp it, so it is provenance, not freshness. The number that answers "how
current is this index" was already persisted as the store's `source_mtime` and simply never
surfaced.

The second half is that the session's instinct was right for a reason it did not have. Every
staleness trigger reduces to `mtime > watermark`, and a history rewrite or a bulk checkout is
exactly the event that falsifies mtime: once any later file pushes the watermark past the rewritten
ones they are unreachable by any trigger, permanently, while the paths still exist so set drift
(SD1-SD4) sees nothing either. No live instance survived re-measurement — a 10,096-file store
re-hashed in full came back clean — so VF2 constructs the state rather than sampling for it, and
asserts both halves: the mtime trigger blind to the file, the hash pass not.
"""
from __future__ import annotations

import json
import os

import pytest

from rag_search.daemon import sweeps
from tests.live._run import run_tool

pytestmark = pytest.mark.live


def _seed(root):
    """A real indexed project with a watermark: two files through sweeps' own index path.

    `index_project` alone leaves `meta.source_mtime` unset, and the watermark is the whole
    subject here — so the pass that stamps it is the one the fixture has to use.
    """
    (root / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "b.py").write_text("def beta():\n    return alpha()\n")
    sweeps._index_files(str(root), [root / "a.py", root / "b.py"])


def _rewrite_beneath_the_watermark(path):
    """Change a file's content while moving its mtime backwards, as a rewrite does."""
    baseline = sweeps._vectors_baseline(str(path.parent))
    assert baseline, "fixture never recorded a watermark, so nothing below it means anything"
    path.write_text("def alpha():\n    return 999\n")
    os.utime(path, (baseline - 100, baseline - 100))
    return baseline


def test_vf1_status_reports_the_watermark_not_just_the_completeness_stamp(
        safe_tmp_path, embedder):
    """VF1: `vectors_current_through` is present and matches the store's own watermark.

    Red before the fix: the payload carried `indexed_at` as its first field and nothing else a
    reader could date the index by.
    """
    from datetime import UTC, datetime

    from rag_search.core.config import ProjectEntry, project_graph_db
    from rag_search.core.registry import remove_project, upsert_project
    from rag_search.graph.store import GraphStore
    from rag_search.server.mcp import overview as overview_tool

    proj = str(safe_tmp_path)
    _seed(safe_tmp_path)
    GraphStore(project_graph_db(proj)).close()  # status fans out over graph stores, not vectors
    # A deliberately old completeness stamp: the point of the field under test is that it says
    # something `indexed_at` cannot, so a fixture where the two agree would prove nothing.
    upsert_project(ProjectEntry(path=proj, enabled=True,
                                indexed_at="2026-01-01T00:00:00+00:00"))
    try:
        result = json.loads(run_tool(overview_tool(proj, "status")))
        assert "vectors_current_through" in result, (
            f"no freshness field in the status payload; keys={list(result)}")
        wm = sweeps._vectors_baseline(proj)
        assert wm, "fixture recorded no watermark"
        assert result["vectors_current_through"] == datetime.fromtimestamp(wm, UTC).isoformat(), (
            f"reported freshness disagrees with the store: {result['vectors_current_through']} "
            f"vs meta.source_mtime {wm}")
        assert result["vectors_current_through"] > result["indexed_at"], (
            "the fixture's watermark is not ahead of its completeness stamp, so this guard "
            f"cannot tell the two apart: {result['vectors_current_through']} vs "
            f"{result['indexed_at']}")
    finally:
        remove_project(proj)


def test_vf2_content_drift_beneath_the_watermark_is_found(safe_tmp_path, embedder):
    """VF2: a file rewritten below the watermark is invisible to mtime and visible to the hash.

    Both halves are load-bearing. The negative arm is what makes this a new trigger rather than a
    second spelling of an existing one: if `_vectors_content_stale` could see this file, the whole
    pass would be dead code.
    """
    _seed(safe_tmp_path)
    drifted = safe_tmp_path / "a.py"
    _rewrite_beneath_the_watermark(drifted)

    assert sweeps._vectors_content_stale(str(safe_tmp_path)) == [], (
        "the mtime trigger saw a file whose mtime moved backwards — the fixture is not "
        "reproducing a rewrite, so the arm below proves nothing")
    found = sweeps._vectors_hash_drift(str(safe_tmp_path))
    assert [p.name for p in found] == ["a.py"], f"expected a.py to drift, got {found}"


def test_vf3_content_drift_converges_once_repaired(safe_tmp_path, embedder):
    """VF3: re-embedding the reported files clears the drift, and the store answers anew."""
    from rag_search.core.config import project_vector_db
    from rag_search.index.store import VectorStore

    _seed(safe_tmp_path)
    _rewrite_beneath_the_watermark(safe_tmp_path / "a.py")
    drifted = sweeps._vectors_hash_drift(str(safe_tmp_path))
    assert drifted, "nothing to repair, so convergence below would be vacuous"

    sweeps._index_files(str(safe_tmp_path), drifted)
    assert sweeps._vectors_hash_drift(str(safe_tmp_path)) == [], (
        "the repair ran and the file still hashes differently from the index")

    vs = VectorStore(project_vector_db(str(safe_tmp_path)), migrate=False)
    try:
        texts = [r[0] for r in vs._con.execute(
            "SELECT content FROM chunks WHERE path LIKE '%a.py'")]
    finally:
        vs.close()
    assert any("999" in t for t in texts), (
        "file_hashes agrees but the chunks still hold the pre-rewrite text — the hash was "
        f"updated without the content: {texts}")


def test_vf4_the_pass_is_bounded_and_the_cursor_rotates(safe_tmp_path, embedder):
    """VF4: one pass re-hashes at most the cap, and the next pass resumes past it.

    Without the cursor the window would restart at the same head every pass, so the tail of a
    large store would be verified never rather than eventually — the exact shape of failure that
    a bounded check silently degrades into. The window is a parameter for exactly this: asserting
    it at the production 500 would need a 501-file fixture to say anything at all.
    """
    _seed(safe_tmp_path)
    for name in ("a.py", "b.py"):
        _rewrite_beneath_the_watermark(safe_tmp_path / name)

    first = sweeps._vectors_hash_drift(str(safe_tmp_path), limit=1)
    assert len(first) == 1, f"the cap of 1 did not bound the pass: {first}"
    second = sweeps._vectors_hash_drift(str(safe_tmp_path), limit=1)
    assert len(second) == 1, f"the cap of 1 did not bound the second pass: {second}"
    assert {p.name for p in first} | {p.name for p in second} == {"a.py", "b.py"}, (
        f"the cursor did not advance: pass one saw {first}, pass two saw {second}")
