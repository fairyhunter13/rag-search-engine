"""The root a model writes, checked against a root it cannot.

Every fixture here is a `tmp_path` directory in the autouse-isolated registry,
so nothing reads the real fleet and no assertion can name a real project. What
is asserted is shape: which of two synthetic roots a pinned workspace reaches.

`mode="lexical"` throughout, and that is load-bearing rather than tidy: it is
what makes the falsification cheap. Delete the check and these calls fall
through into `search.search`, which would load a model on the dense lane.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest
from mcp.server.mcpserver.resolve import ListRoots
from mcp_types import ClientCapabilities, ListRootsResult

from coderag import config, registry, scope, tools


def _project(path: Path, *, enabled: bool = True, indexed: bool = True) -> Path:
    """A registry row shaped like one the indexer wrote, with no store behind it."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    registry.claim(path, direct=True)
    if indexed:
        registry.update(path, indexed_at=1_700_000_000.0, file_count=1, chunk_count=1)
    if not enabled:
        registry.set_enabled(path, False)
    return registry.resolve(path)


@pytest.fixture
def two(tmp_path):
    """Mine and theirs: both registered, both enabled, both indexed."""
    return _project(tmp_path / "mine"), _project(tmp_path / "theirs")


# ------------------------------------------------------------------ the pin


def test_the_workspaces_own_project_is_reachable(two, pin):
    """First, because without it every refusal below is satisfied by a gate that
    refuses everything -- and a gate that refuses everything is not this gate."""
    mine, _ = two
    scope.enforce(mine, pin(mine))


def test_a_root_outside_the_workspace_is_refused(two, pin):
    """The whole requirement in one assertion. `theirs` is registered, enabled
    and indexed, so nothing downstream would stop it; the only thing that does
    is that the caller's workspace does not contain it."""
    mine, theirs = two
    with pytest.raises(scope.ScopeError, match="outside this session's workspace"):
        scope.enforce(theirs, pin(mine))


def test_a_workspace_opened_on_a_subdirectory_reaches_its_project(two, pin):
    """The ancestor arm. An editor opened on `repo/backend` has to be able to
    name `repo`, and it cannot walk out past it: search still requires the row
    to be registered, enabled and indexed, and `/` and `$HOME` never are."""
    mine, _ = two
    scope.enforce(mine, pin(mine / "backend"))


def test_a_sibling_of_the_workspace_is_not_an_ancestor(tmp_path, pin):
    """The ancestor arm is `is_relative_to`, not a shared prefix. `mine-other`
    starts with `mine` as a string and must not resolve as one."""
    mine = _project(tmp_path / "mine")
    other = _project(tmp_path / "mine-other")
    with pytest.raises(scope.ScopeError):
        scope.enforce(other, pin(mine))


# ------------------------------------------------------- the rollout switch


def test_an_absent_pin_is_refused_when_required(two, monkeypatch, pin):
    mine, _ = two
    monkeypatch.setattr(config, "REQUIRE_CLIENT_ROOTS", True)
    with pytest.raises(scope.ScopeError, match="no workspace roots"):
        scope.enforce(mine, pin())


def test_an_absent_pin_passes_while_the_unit_ships_it_off(two, monkeypatch, pin):
    """The rollout, asserted rather than assumed: at `0` an unpinned client is
    exactly as reachable as it was before this module existed."""
    _, theirs = two
    monkeypatch.setattr(config, "REQUIRE_CLIENT_ROOTS", False)
    scope.enforce(theirs, pin())


def test_the_flag_never_softens_a_pin_that_did_arrive(two, monkeypatch, pin):
    """Off is "no pin arrived", not "no checking". Otherwise the rollout switch
    is a permanent bypass any client can trigger by sending an empty list."""
    mine, theirs = two
    monkeypatch.setattr(config, "REQUIRE_CLIENT_ROOTS", False)
    with pytest.raises(scope.ScopeError):
        scope.enforce(theirs, pin(mine))


# ------------------------------------------------------- what search refuses


def test_search_refuses_a_root_outside_the_workspace(two, pin):
    """Through the tool body, because the check being *called* is the half a
    unit test of `enforce` cannot see."""
    mine, theirs = two
    out = tools.search_code("handler", pin(mine), root=str(theirs), mode="lexical")
    assert out["results"] == []
    assert "outside this session's workspace" in out["error"]


def test_search_refuses_a_project_that_was_unflagged(tmp_path, pin):
    """4a. `registry.get` returns disabled rows and unflagging deletes no store,
    so this was searchable by name until the predicate said `enabled`."""
    mine = _project(tmp_path / "mine", enabled=False)
    out = tools.search_code("handler", pin(mine), root=str(mine), mode="lexical")
    assert "is not indexed" in out["error"]


def test_search_refuses_a_project_that_was_registered_but_never_indexed(tmp_path, pin):
    """4b, and the message was already claiming it. Claiming a root registers it
    immediately; the pass that sets `indexed_at` runs in the background."""
    mine = _project(tmp_path / "mine", indexed=False)
    out = tools.search_code("handler", pin(mine), root=str(mine), mode="lexical")
    assert "is not indexed" in out["error"]
    assert f"index(root={str(mine)!r})" in out["error"], "the refusal has to name the fix"


def test_index_is_pinned_too(two, pin):
    """Or the gate on search is a formality: whatever can be indexed can be made
    searchable, and `index` is the tool that sets the flag search reads."""
    mine, theirs = two
    out = tools.index_project(pin(mine), root=str(theirs))
    assert "outside this session's workspace" in out["error"]
    assert registry.get(theirs).indexed_at == 1_700_000_000.0, "the refusal wrote nothing"


def test_an_empty_root_resolves_from_thepin(two, pin):
    """The default was the *daemon's* cwd, which is `$HOME`. That is the habit
    the pin exists to constrain."""
    _, theirs = two
    out = tools.search_code("handler", pin(theirs), mode="lexical")
    assert "outside" not in out.get("error", ""), "root='' did not resolve to the pin"


def test_a_rootless_call_with_no_pin_names_the_fix_rather_than_home():
    """The `$HOME` reply, at both tools. With the cwd fallback a rootless call
    from a client that sends no roots came back as `index(root=$HOME)` --
    advice a real session took. `enforce` never saw it: an unregistered `$HOME`
    fails the indexed gate first, so the error blamed the wrong thing."""
    empty = ListRootsResult(roots=[])
    for out in (tools.search_code("handler", empty), tools.index_project(empty)):
        assert "no workspace root" in out["error"], out
        assert str(Path.home()) not in out["error"]


# ------------------------------------------------------- when the ask is made


class _Ctx:
    """Only the two attributes `_ask` reads. A real `Context` would need a live
    session, which is the thing this branch decides not to talk to."""

    def __init__(self, caps, version):
        self.client_capabilities = caps
        self.protocol_version = version


_ROOTS = ClientCapabilities(roots={"listChanged": True})


@pytest.mark.parametrize(
    "caps,version,reason",
    [
        (None, scope.MRTR, "roots capability"),
        (ClientCapabilities(), scope.MRTR, "roots capability"),
        (_ROOTS, None, "below"),
        (_ROOTS, "2025-06-18", "below"),
    ],
    ids=["no capabilities", "no roots capability", "no version", "below the era"],
)
def test_no_ask_is_made_where_the_answer_cannot_arrive(caps, version, reason, caplog):
    """Below `MRTR` a stateless transport is built `can_send_request=False` and
    asking raises `NoBackChannelError`, which would take down every call rather
    than falling back to no pin.

    The logged reason is asserted because it is the whole of the rollout's
    evidence: `enforce`'s count says a pin did not arrive and never which of
    these two branches sent it back empty.
    """
    with caplog.at_level(logging.INFO, logger=scope.log.name):
        assert scope._ask(_Ctx(caps, version)) == ListRootsResult(roots=[])
    assert reason in caplog.text, caplog.text


def test_the_ask_is_made_when_the_client_can_answer_it():
    """The control. Without it the four above are satisfied by an `_ask` that
    never asks anyone, which is a pin that never arrives."""
    assert isinstance(scope._ask(_Ctx(_ROOTS, scope.MRTR)), ListRoots)
