"""Federation architecture invariant tests — no mocks, real daemon + GPU.

Proves §13 invariants from docs/architecture/federation-ops-and-invariants.md.
pause_sweeps (autouse, session) disables reconcile; tests drive the pipeline directly.
safe_tmp_path keeps roots off /tmp/.cache.
"""
from __future__ import annotations

import asyncio
import json
import shutil

import pytest

from rag_search.core.config import index_dir

pytestmark = pytest.mark.live


def _federate(base):
    uid = str(id(base))[-6:]
    marker = f"rse_arch_{uid}"
    root = base / "root"
    member = base / "member-repo"
    root.mkdir()
    member.mkdir()
    (member / f"{marker}.py").write_text(f"def {marker}(): pass\n")
    (root / "readme.txt").write_text("root\n")
    (root / "link").symlink_to(member)
    return root, member, marker


def _clean(paths):
    from rag_search.core.registry import remove_project
    for p in paths:
        remove_project(str(p))
        shutil.rmtree(index_dir(str(p)), ignore_errors=True)


def test_gdup_duplicate_symlink_members_deduped(safe_tmp_path):
    """G-DUP: a member symlinked from several locations must be counted once at every
    layer: discover_members dedups at the source, index_members therefore stores a
    dedup'd root.federation (no N× bloat), and expand_federation dedups as defense."""
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import get_project, upsert_project
    from rag_search.daemon.federation import (
        discover_members,
        expand_federation,
        index_members,
    )

    root = safe_tmp_path / "root"
    member = safe_tmp_path / "member-repo"
    root.mkdir()
    member.mkdir()
    (member / "a.py").write_text("def f(): pass\n")
    (root / "link_one").symlink_to(member)
    (root / "link_two").symlink_to(member)
    (root / "link_three").symlink_to(member)
    _clean([root, member])
    try:
        # Source dedup: three symlinks → one member, not three.
        assert discover_members(str(root)).count(str(member)) == 1, "discover_members must dedup"

        upsert_project(ProjectEntry(path=str(root), enabled=True))
        index_members(str(root))
        # Stored federation must not be N× bloated.
        stored = get_project(str(root)).federation
        assert stored.count(str(member)) == 1, f"stored federation bloated: {stored}"
        # Union deduped too (defense in depth).
        union = expand_federation(str(root))
        assert union.count(str(member)) == 1, f"member counted more than once: {union}"
    finally:
        _clean([root, member])


def test_inv1_no_inlining(safe_tmp_path):
    """Invariant #1: root index must not contain symbols from the member path."""
    from rag_search.daemon.federation import index_members
    from rag_search.daemon.sweeps import _index_project
    from rag_search.graph.store import GraphStore

    root, member, _marker = _federate(safe_tmp_path)
    _clean([root, member])
    try:
        index_members(str(root))
        _index_project(str(root))
        gs = GraphStore(index_dir(str(root)) / "graph.db")
        try:
            files = {r[0] for r in gs._con.execute("SELECT file FROM symbols").fetchall()}
        finally:
            gs.close()
        member_str = str(member)
        leaked = [f for f in files if f.startswith(member_str)]
        assert not leaked, f"member files leaked into root index: {leaked}"
    finally:
        _clean([root, member])


def test_inv2_members_first_class(safe_tmp_path):
    """Invariant #2: a member is registered, enabled, and reachable BOTH ways.

    "First-class" used to be asserted with an unscoped `mcp_search(marker, "code", None)`,
    i.e. "findable without naming any project". T2 deliberately deleted that fleet-wide
    fallback — unscoped now returns `{"error": "project_path required", "candidates": [...]}`
    — so this test had been asserting the absence of a feature that was removed on purpose,
    and `@pytest.mark.slow` kept it from ever saying so. Restoring the unscoped call would be
    re-asserting the deleted behaviour; the honest reading of "first-class" under scatter-
    gather is that the member is reachable through the federation union *and* on its own,
    which is what the two arms below check. The second arm is not redundant: a member that
    only ever answered through its root would be an inlined shard, not a first-class project.
    """
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import get_project, upsert_project
    from rag_search.daemon.federation import index_members
    from rag_search.daemon.sweeps import _index_project
    from rag_search.server.mcp import search as mcp_search

    root, member, marker = _federate(safe_tmp_path)
    _clean([root, member])
    try:
        upsert_project(ProjectEntry(path=str(root), enabled=True))
        n = index_members(str(root))
        assert n == 1, f"expected 1 new member, got {n}"
        assert get_project(str(member)) is not None
        _index_project(str(member))
        # A LIST — `search`'s third parameter is `project_paths`, plural. Passing the bare
        # string iterates it character by character, and each character is a *relative* path
        # that resolve_registered_root then resolves against the cwd, so a run from inside a
        # registered repo answers confidently out of that repo instead.
        for label, scope_path in (("root union", str(root)), ("own path", str(member))):
            data = json.loads(asyncio.run(mcp_search(marker, "code", [scope_path])))
            files = [r["path"] for r in data.get("results", [])]
            assert any(str(member) in f for f in files), \
                f"member unreachable via {label}: {data.get('error') or files}"
    finally:
        _clean([root, member])


def test_inv3_federation_authoritative(safe_tmp_path):
    """Invariant #3: root.federation set after index_members; idempotent on rerun."""
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import get_project, upsert_project
    from rag_search.daemon.federation import index_members

    root, member, _m = _federate(safe_tmp_path)
    _clean([root, member])
    try:
        upsert_project(ProjectEntry(path=str(root), enabled=True))
        index_members(str(root))
        assert str(member) in get_project(str(root)).federation
        n2 = index_members(str(root))
        assert n2 == 0, f"expected 0 new on rerun, got {n2}"
        assert str(member) in get_project(str(root)).federation
    finally:
        _clean([root, member])


def test_inv6_forbidden_root():
    """Invariant #6: registering a /tmp root must be rejected."""
    from rag_search.server.mcp import index as mcp_index
    path = "/tmp/rse-arch-forbid-test"
    result = json.loads(asyncio.run(mcp_index(path, enabled=True)))
    assert result.get("status") == "forbidden", f"expected forbidden, got {result}"
    from rag_search.core.registry import get_project
    assert get_project(path) is None


def test_inv8_cascade_remove(safe_tmp_path):
    """Invariant #8: index(root, False) removes root+member from registry and storage."""
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import get_project, upsert_project
    from rag_search.daemon.federation import index_members
    from rag_search.server.mcp import index as mcp_index

    root, member, _m = _federate(safe_tmp_path)
    _clean([root, member])
    try:
        upsert_project(ProjectEntry(path=str(root), enabled=True))
        index_members(str(root))
        # confirm_members: the cascade is what this invariant is about, and removing a root now
        # previews the fan-out first. IR1/IR4 own that gate; passing it here keeps this test on its
        # own claim instead of silently becoming a second, weaker copy of the confirmation test.
        result = json.loads(asyncio.run(mcp_index(str(root), enabled=False, confirm_members=True)))
        assert result.get("status") == "removed", f"unexpected: {result}"
        assert str(member) in result.get("members_removed", [])
        assert get_project(str(root)) is None
        assert get_project(str(member)) is None
        assert not index_dir(str(member)).exists()
    finally:
        _clean([root, member])
