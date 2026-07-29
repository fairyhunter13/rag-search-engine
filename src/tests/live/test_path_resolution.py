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
        # A `get_project(raw) is not None` precondition stood here and went red intermittently:
        # the registry is one shared file and the live daemon's reconcile calls list_projects()
        # on its own schedule, so it can re-key the entry before this process looks. That is the
        # self-heal working, not failing. The end state below is the contract, and it can only
        # hold if the seeding landed — nothing else in this test registers `canon`.
        list_projects()  # triggers _migrate()'s self-heal, if the daemon has not already
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
            # Renamed from kb_state with tier 3 (HR7, _overview.py:155-161).
            assert ov.get("index_state") is not None, (probe, ov)

            answer = asyncio.run(mcp_ask(marker, probe, "all"))
            assert "not indexed" not in answer.lower(), (probe, answer)

            gd = json.loads(asyncio.run(mcp_graph(marker, probe, "definition", "")))
            assert gd.get("resolved_project") == str(member), (probe, gd)
            assert any(m.get("name") == marker for m in gd.get("matches", [])), (probe, gd)
    finally:
        _clean([root, member])


class _UrlCtx:
    """A tool's view of one HTTP request, carrying `?project=` from the MCP server URL.

    Stands in for the transport, not for a model — nothing here reaches the GPU, so the
    zero-fake policy is untouched. The MCP SDK populates `RequestContext.request` from the
    HTTP request (`mcp/server/lowlevel/server.py:765`); this reproduces exactly the attribute
    chain `_scope_from_request` reads, and nothing else.
    """

    def __init__(self, project: str | None = None):
        from types import SimpleNamespace
        params = {} if project is None else {"project": project}
        self.request_context = SimpleNamespace(request=SimpleNamespace(query_params=params))


def test_t1_url_scope_is_read_when_the_client_offers_no_roots(safe_tmp_path):
    """T1: `?project=` on the server URL scopes a call, with no roots capability involved.

    This is the rung that makes scoping a property of the configuration. `_UrlCtx` advertises
    no roots at all, which is precisely the client that used to get a 160-store fleet scan.
    """
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import upsert_project
    from rag_search.server.mcp import _default_or_error, _scope_from_request

    root, member, sub, _marker = _federate(safe_tmp_path)
    _clean([root, member])
    try:
        upsert_project(ProjectEntry(path=str(member), enabled=True))
        assert _scope_from_request(_UrlCtx(str(member))) == (str(member), None)
        # Same resolution the arguments get: a subdir maps to its enclosing registered project.
        assert _scope_from_request(_UrlCtx(str(sub))) == (str(member), None)
        # No scope on the URL is not an error — it falls through to the roots rung.
        assert _scope_from_request(_UrlCtx()) == ("", None)
        assert _scope_from_request(None) == ("", None)

        # The ladder prefers an explicit argument over the URL, so a caller on a scoped
        # connection can still ask about another project.
        assert asyncio.run(_default_or_error(_UrlCtx(str(member)), "")) == (str(member), None)
        assert asyncio.run(_default_or_error(_UrlCtx(str(member)), str(root))) == (
            str(root), None), "explicit argument must outrank the URL"
    finally:
        _clean([root, member])


def test_t1_unregistered_url_scope_fails_loud(safe_tmp_path):
    """A typo in the server URL must be an error, not zero stores and a confident 'no matches'.

    Without this the T3 config migration has no failure signal: a mistyped `?project=` opens no
    stores, and `search` answers every question with an empty result list that reads like a
    correct negative.
    """
    from rag_search.server.mcp import _scope_from_request

    scoped, err = _scope_from_request(_UrlCtx(str(safe_tmp_path / "not-a-project")))
    assert scoped == ""
    assert err is not None, "an unregistered ?project= resolved silently"
    payload = json.loads(err)
    assert "not-a-project" in payload["error"]
    assert payload["candidates"], "the error must name registered projects to fix the URL"


def test_t2_unscoped_search_fails_loud_instead_of_scanning_the_fleet():
    """T2: `search` with no project and no inferable scope must error, not search all 160.

    Red twice, and the two are worth separating. Against the *deployed* daemon on 2026-07-29
    the same query reported `projects_searched` of ~160 in **164.78 s** unscoped versus **1**
    in **7.01 s** scoped, and its top hit for a question about *this* repo's reconcile loop was
    `redacted-name-7/redacted-name-13-lp` JavaScript. Against the pre-change code in-process, this gate failed
    by *reaching the GPU at all* — an ONNX BFC arena OOM, because it genuinely set out to
    search the fleet while the repair held the card. Neither red is the assertion below
    tripping, so state it plainly: what this pins is that the unscoped path now returns before
    it opens a single store.

    `_UrlCtx()` carries neither roots nor `?project=`, so it lands on the last rung.
    """
    from rag_search.server.mcp import search as mcp_search

    payload = json.loads(asyncio.run(
        mcp_search("reconcile loop", "code", None, 8, "compact", _UrlCtx())))
    assert "error" in payload, f"unscoped search still returned results: {str(payload)[:300]}"
    assert "project_path required" in payload["error"]
    assert "results" not in payload
    assert payload["candidates"], "the error must list candidates the caller can pick from"


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
