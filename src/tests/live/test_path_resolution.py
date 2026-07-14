"""Live e2e tests for path resolution (symlink/subdir/trailing-slash) — S1/S2/S3 fix.

resolve_registered_root() (read paths) / canonicalize_path() (index tool, write-only) in
core/registry.py fix registry-key / index_dir() misses on non-canonical paths.
"""
from __future__ import annotations

import asyncio
import json
import shutil

import pytest

from rag_search.core.config import index_dir

pytestmark = pytest.mark.live


def _federate(base):
    """root/member + symlink into member + a subdir inside member, for S1/S2 probes."""
    uid = str(id(base))[-6:]
    marker = f"rse_path_{uid}"
    root = base / "root"
    member = base / "member-repo"
    sub = member / "sub" / "pkg"
    sub.mkdir(parents=True)
    root.mkdir()
    (sub / f"{marker}.py").write_text(f"def {marker}(): pass\n")
    (root / "readme.txt").write_text("root\n")
    (root / "link").symlink_to(member)
    return root, member, sub, marker


def _clean(paths):
    from rag_search.core.registry import remove_project
    for p in paths:
        remove_project(str(p))
        shutil.rmtree(index_dir(str(p)), ignore_errors=True)


def test_resolver_contract(safe_tmp_path):
    """resolve_registered_root: exact->exact, symlink->self, subdir->root, unknown->canonical,
    empty->empty. canonicalize_path: OSError->identity (empty here; no fast way to force OSError)."""
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import (
        canonicalize_path,
        resolve_registered_root,
        upsert_project,
    )

    root, member, sub, _marker = _federate(safe_tmp_path)
    _clean([root, member])
    try:
        upsert_project(ProjectEntry(path=str(root), enabled=True))
        upsert_project(ProjectEntry(path=str(member), enabled=True))
        assert resolve_registered_root(str(root)) == str(root)
        # symlink into a registered member resolves to the member itself, not the root
        assert resolve_registered_root(str(root / "link")) == str(member)
        # subdir of the member (not itself registered) resolves to its enclosing root (member)
        assert resolve_registered_root(str(sub)) == str(member)
        # trailing slash is not a distinct path after canonicalization
        assert resolve_registered_root(str(root) + "/") == str(root)
        unknown = str(safe_tmp_path / "unregistered")
        assert resolve_registered_root(unknown) == canonicalize_path(unknown)
        assert resolve_registered_root("") == ""
        assert canonicalize_path("") == ""
    finally:
        _clean([root, member])


def test_migrate_rekeys_raw_symlink_registration(safe_tmp_path):
    """Self-heal: an entry keyed by a raw (unresolved) path is re-keyed to its canonical
    real path the next time list_projects() runs _migrate(), and the stale raw key is gone."""
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import (
        canonicalize_path,
        get_project,
        list_projects,
        upsert_project,
    )

    root, member, _sub, _marker = _federate(safe_tmp_path)
    _clean([root, member])
    try:
        raw = str(root / "link")  # symlink path, deliberately not canonicalized
        upsert_project(ProjectEntry(path=raw, enabled=True))
        assert get_project(raw) is not None  # sanity: seeded under the raw key
        list_projects()  # triggers _migrate()'s self-heal
        canon = canonicalize_path(raw)
        assert canon == str(member)
        assert get_project(canon) is not None
        assert get_project(raw) is None
    finally:
        _clean([root, member, raw])


def test_s3_index_tool_canonicalizes_symlink(safe_tmp_path):
    """S3: the MCP index tool must canonicalize before registering — a symlink path is stored
    under its canonical real-path key, matching what CLI `init` would produce."""
    from rag_search.core.registry import canonicalize_path, get_project
    from rag_search.server.mcp import index as mcp_index

    root, member, _sub, _marker = _federate(safe_tmp_path)
    _clean([root, member])
    try:
        raw = str(root / "link")
        result = json.loads(asyncio.run(mcp_index(raw, enabled=True)))
        assert result.get("status") == "flagged", result
        canon = canonicalize_path(raw)
        assert canon == str(member)
        assert get_project(canon) is not None
        assert get_project(raw) is None
    finally:
        _clean([root, member])


@pytest.mark.slow
def test_infer_default_project(safe_tmp_path):
    """infer_default_project: a root that is/encloses exactly one registered project -> that
    project; an unregistered root -> (None, candidates); two distinct registered roots ->
    (None, ...). This is the fix for the empty-project_path -> arbitrary projects[0] bug."""
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import infer_default_project, upsert_project

    root, member, sub, _marker = _federate(safe_tmp_path)
    _clean([root, member])
    try:
        upsert_project(ProjectEntry(path=str(root), enabled=True))
        upsert_project(ProjectEntry(path=str(member), enabled=True))
        assert infer_default_project([str(root)])[0] == str(root)
        assert infer_default_project([str(sub)])[0] == str(member)            # subdir -> member
        assert infer_default_project([str(root / "link")])[0] == str(member)  # symlink -> member
        chosen, cands = infer_default_project([str(safe_tmp_path / "nope")])  # unregistered
        assert chosen is None and str(root) in cands and str(member) in cands
        assert infer_default_project([str(root), str(member)])[0] is None     # ambiguous
    finally:
        _clean([root, member])


def test_migrate_prunes_nonexistent_entry(safe_tmp_path):
    """_migrate self-heals: a registered path that no longer exists on disk is dropped on load."""
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import list_projects, upsert_project

    ghost = safe_tmp_path / "ghost-repo"
    ghost.mkdir()
    upsert_project(ProjectEntry(path=str(ghost), enabled=True))
    assert any(p.path == str(ghost) for p in list_projects())
    shutil.rmtree(ghost)
    assert not any(p.path == str(ghost) for p in list_projects())


def test_s1_overview_ask_graph_resolve_symlink_subdir_trailing_slash(safe_tmp_path):
    """S1: overview/ask/graph on a symlinked member, a subdir inside it, or a trailing-slash
    path must all resolve to the member's own KB — never 'no project available'/'not indexed'."""
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import upsert_project
    from rag_search.daemon.sweeps import _index_project
    from rag_search.server.mcp import ask as mcp_ask
    from rag_search.server.mcp import graph as mcp_graph
    from rag_search.server.mcp import overview as mcp_overview

    root, member, sub, marker = _federate(safe_tmp_path)
    _clean([root, member])
    try:
        upsert_project(ProjectEntry(path=str(member), enabled=True))
        _index_project(str(member))
        for probe in (str(root / "link"), str(sub), str(member) + "/"):
            ov = json.loads(asyncio.run(mcp_overview(probe, "status")))
            assert ov.get("resolved_project") == str(member), (probe, ov)
            assert ov.get("kb_state") is not None, (probe, ov)

            answer = asyncio.run(mcp_ask(marker, probe, "all"))
            assert "not indexed" not in answer.lower(), (probe, answer)

            gd = json.loads(asyncio.run(mcp_graph(marker, probe, "definition", "")))
            assert gd.get("resolved_project") == str(member), (probe, gd)
            assert any(m.get("name") == marker for m in gd.get("matches", [])), (probe, gd)
    finally:
        _clean([root, member])


@pytest.mark.slow
def test_s2_search_symlinked_member_does_not_fanout(safe_tmp_path):
    """S2: search() scoped to a symlinked member must resolve to that member alone, not fan
    out to the enclosing federation root's whole member list."""
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import upsert_project
    from rag_search.daemon.federation import index_members
    from rag_search.daemon.sweeps import _index_project
    from rag_search.server.mcp import search as mcp_search

    root, member, _sub, marker = _federate(safe_tmp_path)
    _clean([root, member])
    try:
        upsert_project(ProjectEntry(path=str(root), enabled=True))
        index_members(str(root))
        _index_project(str(member))
        data = json.loads(asyncio.run(mcp_search(marker, "code", [str(root / "link")])))
        assert data.get("projects_searched") == [str(member)], data
        files = [r["path"] for r in data.get("results", [])]
        assert any(str(member) in f for f in files), (marker, files)
    finally:
        _clean([root, member])
