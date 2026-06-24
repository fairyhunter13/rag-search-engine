"""Live tests: freshness-guard _reusable_existing + templated-placeholder fallback consistency.

FG1 — _reusable_existing keeps real-prose rows and drops templated rows.
FG2 — the templated fallback emitted by build_federation_hierarchy contains _TEMPLATED_MARK.
FG3 — after a fully-templated (OSE_WIKI_LLM=0) L3 build, _reusable_existing returns empty.

GPU-free and deterministic (no DeepSeek calls, no embed calls).
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from opencode_search.core.config import ProjectEntry, project_graph_db
from opencode_search.core.registry import remove_project, upsert_project
from opencode_search.graph.store import GraphStore
from opencode_search.kb.federation_hierarchy import _TEMPLATED_MARK, _reusable_existing

pytestmark = pytest.mark.live

_SAFE_BASE = Path.home() / ".local" / "share" / "ocs-test-dirs"


def _make_fg_member(parent: Path, name: str, stype: str) -> str:
    d = parent / name
    d.mkdir(parents=True)
    gdb = project_graph_db(str(d))
    gdb.parent.mkdir(parents=True, exist_ok=True)
    gs = GraphStore(gdb)
    try:
        gs.upsert_community(1, level=2, title=f"{name}-domain",
                            summary="test domain", member_count=3, narrated=1)
        gs._con.execute("UPDATE communities SET semantic_type=? WHERE id=1", (stype,))
        gs.commit()
    finally:
        gs.close()
    upsert_project(ProjectEntry(path=str(d), enabled=True))
    return str(d)


def _build_templated(root: str) -> None:
    from opencode_search.kb.federation_hierarchy import build_federation_hierarchy
    prev = os.environ.get("OSE_WIKI_LLM")
    os.environ["OSE_WIKI_LLM"] = "0"
    try:
        build_federation_hierarchy(root)
    finally:
        if prev is None:
            os.environ.pop("OSE_WIKI_LLM", None)
        else:
            os.environ["OSE_WIKI_LLM"] = prev


def _l3_title_summary(root: str) -> list[tuple[str, str | None]]:
    gs = GraphStore(project_graph_db(root))
    try:
        return gs._con.execute(
            "SELECT title, summary FROM communities WHERE level>=3 ORDER BY id"
        ).fetchall()
    finally:
        gs.close()


def _make_root(base: Path, members: list[str]) -> str:
    root = str(base / "root")
    Path(root).mkdir()
    upsert_project(ProjectEntry(path=root, enabled=True, federation=members))
    gdb = project_graph_db(root)
    gdb.parent.mkdir(parents=True, exist_ok=True)
    GraphStore(gdb).close()
    return root


# ---------------------------------------------------------------------------
# FG1 — unit: _reusable_existing keeps real prose, drops templated
# ---------------------------------------------------------------------------

def test_fg1_reusable_existing_filters_correctly():
    """FG1: _reusable_existing keeps real-prose rows and excludes templated ones."""
    real = "The Business Process domain manages campaigns and promotions across services."
    templated = f"Cross-service Feature domain spanning 5 {_TEMPLATED_MARK}."
    rows: list[tuple[str, str | None]] = [
        ("Federation: Business Process", real),
        ("Federation: Feature", templated),
        ("Federation: Test", None),
        ("Federation: Utility", ""),
    ]
    out = _reusable_existing(rows)
    assert "Federation: Business Process" in out and out["Federation: Business Process"] == real
    assert "Federation: Feature" not in out, "templated row must be excluded"
    assert "Federation: Test" not in out
    assert "Federation: Utility" not in out


# ---------------------------------------------------------------------------
# FG2 — integration: templated fallback contains _TEMPLATED_MARK
# ---------------------------------------------------------------------------

def test_fg2_templated_fallback_contains_mark():
    """FG2: build_federation_hierarchy(OSE_WIKI_LLM=0) writes summaries containing _TEMPLATED_MARK."""
    _SAFE_BASE.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(dir=_SAFE_BASE))
    m1 = _make_fg_member(base, "m1", "feature")
    m2 = _make_fg_member(base, "m2", "test")
    root = _make_root(base, [m1, m2])
    try:
        _build_templated(root)
        rows = _l3_title_summary(root)
        assert rows, "must have written L3 rows"
        for title, summary in rows:
            assert summary and _TEMPLATED_MARK in summary, (
                f"{title!r} must contain _TEMPLATED_MARK; got {summary!r}"
            )
    finally:
        for p in (root, m1, m2):
            remove_project(p)
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# FG3 — round-trip: after a fully-templated build, nothing is reusable
# ---------------------------------------------------------------------------

def test_fg3_templated_build_produces_no_reusable_rows():
    """FG3: after a fully-templated L3 build, _reusable_existing returns empty — nothing freezes."""
    _SAFE_BASE.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(dir=_SAFE_BASE))
    m1 = _make_fg_member(base, "m1", "domain")
    m2 = _make_fg_member(base, "m2", "utility")
    root = _make_root(base, [m1, m2])
    try:
        _build_templated(root)
        rows = _l3_title_summary(root)
        reusable = _reusable_existing(rows)
        assert not reusable, (
            f"fully-templated build must yield no reusable rows; got {reusable}"
        )
    finally:
        for p in (root, m1, m2):
            remove_project(p)
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# FG4 — per-theme child_sig: order-independent, changes on membership change
# ---------------------------------------------------------------------------

def test_fg4_per_theme_child_sig_drives_reuse():
    """FG4: _child_sig changes when theme membership changes, is order-independent."""
    from opencode_search.kb.federation_hierarchy import _child_sig
    s1 = _child_sig(["svc-a", "svc-b"])
    s2 = _child_sig(["svc-a", "svc-b", "svc-c"])
    s3 = _child_sig(["svc-b", "svc-a"])
    assert s1 != s2, "adding a member must change child_sig"
    assert s1 == s3, "child_sig must be order-independent (sorted)"


# ---------------------------------------------------------------------------
# FG5 — watcher-effectiveness: content sig gates reuse, not the clock
# ---------------------------------------------------------------------------

def test_fg5_content_sig_gates_not_time():
    """FG5: build_federation_hierarchy re-narrates a theme whose child set changed,
    even called back-to-back (no 1800 s wall-clock gate)."""
    _SAFE_BASE.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(dir=_SAFE_BASE))
    m1 = _make_fg_member(base, "m1", "feature")
    m2 = _make_fg_member(base, "m2", "domain")
    root = _make_root(base, [m1, m2])
    try:
        from opencode_search.kb.federation_hierarchy import build_federation_hierarchy
        _build_templated(root)
        rows_before = _l3_title_summary(root)
        # Extend the 'feature' theme by adding a new L2 community in m1.
        gs = GraphStore(project_graph_db(m1))
        try:
            gs.upsert_community(10002, level=2, title="m1-extra",
                                summary="extra", member_count=1, narrated=1)
            gs._con.execute(
                "UPDATE communities SET semantic_type='feature' WHERE id=10002")
            gs.commit()
        finally:
            gs.close()
        build_federation_hierarchy(root)
        rows_after = _l3_title_summary(root)
        assert rows_before != rows_after or len(rows_after) != len(rows_before), (
            "must re-narrate when child membership changes (content sig gate, not clock)"
        )
    finally:
        for p in (root, m1, m2):
            remove_project(p)
        shutil.rmtree(base, ignore_errors=True)
