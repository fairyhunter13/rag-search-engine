"""Logical-entity invariant tests: root+members behave as one entity for ALL query surfaces.

Proves that graph, overview, and ask/search fan out across the federation,
treating root+members as a single logical repository.

Invariant #4: search([root]) + ask(root) reach member content.
Invariant #5: graph(symbol, root) resolves symbols from member graph.db.
Invariant #7: overview(status, root) aggregates counts across members and includes members[].
"""
from __future__ import annotations

import asyncio
import json
import shutil

import pytest

from rag_search.core.config import index_dir

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# Shared test helpers (mirrors test_federation_architecture._federate/_clean)
# ---------------------------------------------------------------------------

def _federate(base):
    uid = str(id(base))[-6:]
    marker = f"ocs_le_{uid}"
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


# ---------------------------------------------------------------------------
# Invariant #4: search + ask fan out through expand_federation to members
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_inv4_root_scoped_search_fanout(safe_tmp_path):
    """Invariant #4: search([root]) and ask(root) fan out to member content."""
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import upsert_project
    from rag_search.daemon.federation import index_members
    from rag_search.daemon.sweeps import _index_project
    from rag_search.server.mcp import ask as mcp_ask
    from rag_search.server.mcp import search as mcp_search

    root, member, marker = _federate(safe_tmp_path)
    _clean([root, member])
    try:
        upsert_project(ProjectEntry(path=str(root), enabled=True))
        index_members(str(root))          # registers member + sets root.federation
        _index_project(str(root))
        _index_project(str(member))       # marker lives only in member

        # search scoped to root must reach member's marker
        data = json.loads(asyncio.run(mcp_search(marker, "code", [str(root)])))
        files = [r["path"] for r in data.get("results", [])]
        assert any(str(member) in f for f in files), \
            f"member not in root-scoped search results: {files}"
        assert any(str(member) in p for p in data.get("projects_searched", [])), \
            f"member not listed in projects_searched: {data.get('projects_searched')}"

        # ask scoped to root must surface content (non-empty assembled context)
        answer = asyncio.run(mcp_ask(marker, str(root), "all"))
        assert answer and len(answer.strip()) > 10, \
            f"ask(root) returned empty context: {answer!r}"
    finally:
        _clean([root, member])


# ---------------------------------------------------------------------------
# Invariant #5: graph(definition) resolves symbols from member's graph.db
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_inv5_graph_definition_fanout(safe_tmp_path):
    """Invariant #5: graph(symbol, root) unions definition matches across members."""
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import upsert_project
    from rag_search.daemon.federation import index_members
    from rag_search.daemon.sweeps import _index_project
    from rag_search.server.mcp import graph as mcp_graph

    root, member, marker = _federate(safe_tmp_path)
    _clean([root, member])
    try:
        upsert_project(ProjectEntry(path=str(root), enabled=True))
        index_members(str(root))
        _index_project(str(root))
        _index_project(str(member))

        data = json.loads(asyncio.run(mcp_graph(marker, str(root), "definition")))
        matches = data.get("matches", [])
        member_matches = [m for m in matches if str(member) in m.get("file", "")]
        assert member_matches, (
            f"graph(definition, root) did not find '{marker}' in member's graph.db. "
            f"All matches: {matches}"
        )
    finally:
        _clean([root, member])


# ---------------------------------------------------------------------------
# Invariant #7: overview(status, root) aggregates counts + includes members[]
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_inv7_overview_status_aggregates(safe_tmp_path):
    """Invariant #7: overview(status, root) sums symbols+communities across members."""
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import upsert_project
    from rag_search.daemon.federation import index_members
    from rag_search.daemon.sweeps import _index_project
    from rag_search.server.mcp import overview as mcp_overview

    root, member, _marker = _federate(safe_tmp_path)
    _clean([root, member])
    try:
        upsert_project(ProjectEntry(path=str(root), enabled=True))
        index_members(str(root))
        _index_project(str(root))
        _index_project(str(member))

        status = json.loads(asyncio.run(mcp_overview(str(root), "status")))
        # members[] must include the member path
        member_paths = [m["path"] for m in status.get("members", [])]
        assert str(member) in member_paths, \
            f"overview(status) missing member in members[]: {status}"
        # symbols must be positive (member has at least the marker function)
        assert status.get("symbols", 0) > 0, \
            f"overview(status) symbols=0, expected ≥1: {status}"
        # root-only file_count must still exist (S6a invariant preserved)
        assert "file_count" in status, \
            f"overview(status) missing root file_count field: {status}"
    finally:
        _clean([root, member])
