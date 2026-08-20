"""The root a model writes, checked against a root it cannot.

Every fixture here is a `tmp_path` directory in the autouse-isolated registry,
so nothing reads the real fleet and no assertion can name a real project. What
is asserted is shape: which of two synthetic roots a pinned workspace reaches.

`mode="lexical"` throughout, and that is load-bearing rather than tidy: it is
what makes the falsification cheap. Delete the check and these calls fall
through into `search.search`, which would load a model on the dense lane.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.resolve import ListRoots
from mcp_types import CLIENT_INFO_META_KEY, ClientCapabilities, ListRootsResult

from coderag import config, federation, pinledger, registry, scope, tools


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


# ------------------------------------------------- the pin and federation


def test_a_pinned_call_reaches_a_member_that_sits_outside_the_pin(tmp_path, pin):
    """The design claim the constraint doc is built on, and nothing tested it:
    members sit outside the workspace by design and are reached *through* the
    root. A change making `expand` pin-filtered would break it silently."""
    member = _project(tmp_path / "outside" / "member")
    root = _project(tmp_path / "root")
    (root / "link").symlink_to(member)
    federation.register(root)

    scope.enforce(root, pin(root))  # the call itself is in the workspace
    assert not member.is_relative_to(root), "the member has to be outside for this to mean anything"
    assert federation.expand(root) == [root, member]


def test_reach_stops_at_one_level(tmp_path, pin):
    """The other direction: a change making `expand` transitive breaks the same
    design and the suite stayed green for both."""
    third = _project(tmp_path / "outside" / "third")
    member = _project(tmp_path / "outside" / "member")
    (member / "link").symlink_to(third)
    root = _project(tmp_path / "root")
    (root / "link").symlink_to(member)
    federation.register(member)
    federation.register(root)

    assert federation.expand(root) == [root, member], "A federating B federating C reached C"


def test_a_member_cannot_be_named_directly_from_a_root_pinned_session(tmp_path, pin):
    """Reachable through the root is not the same as nameable. Containment is
    what the pin buys, and a member outside the workspace is outside it."""
    member = _project(tmp_path / "outside" / "member")
    root = _project(tmp_path / "root")
    (root / "link").symlink_to(member)
    federation.register(root)

    with pytest.raises(scope.ScopeError, match="outside this session's workspace"):
        scope.enforce(member, pin(root))


def test_a_workspace_on_a_subdirectory_reaches_the_projects_the_parent_federates(tmp_path, pin):
    """The ancestor arm composed with federation, which is how every editor
    opened on `repo/backend` actually calls this."""
    member = _project(tmp_path / "outside" / "member")
    root = _project(tmp_path / "root")
    (root / "link").symlink_to(member)
    federation.register(root)
    sub = root / "backend"
    sub.mkdir()

    scope.enforce(root, pin(sub))
    assert member in federation.expand(root)


# ------------------------------------------------------- when the ask is made


class _Ctx:
    """Only the attributes the resolver reads, but carrying what the wire does.

    The `_meta` mapping is the real one, under the reserved key and in the
    camelCase the wire uses: the previous fake mirrored `params.clientInfo`,
    which is a serialization alias and not the attribute, so it agreed with a
    read that named nobody in production. A fake that spells the field the way
    the code under test spells it cannot fail.
    """

    def __init__(self, caps, version, client=None):
        self.client_capabilities = caps
        self.protocol_version = version
        meta = {CLIENT_INFO_META_KEY: {"name": client[0], "version": client[1]}} if client else {}
        self.request_context = SimpleNamespace(meta=meta, request=None)
        self.session = SimpleNamespace(client_params=None)


_ROOTS = ClientCapabilities(roots={"listChanged": True})


@pytest.mark.parametrize(
    "caps,version,reason",
    [
        (None, scope.MRTR, "branch=no-caps"),
        (ClientCapabilities(), scope.MRTR, "branch=no-caps"),
        (_ROOTS, None, "branch=below-era"),
        (_ROOTS, "2025-06-18", "branch=below-era"),
        (None, "2025-06-18", "branch=legacy-path"),
    ],
    ids=[
        "no capabilities",
        "no roots capability",
        "no version",
        "below the era",
        "the legacy stateless path",
    ],
)
def test_no_ask_is_made_where_the_answer_cannot_arrive(caps, version, reason, caplog):
    """Below `MRTR` a stateless transport is built `can_send_request=False` and
    asking raises `NoBackChannelError`, which would take down every call rather
    than falling back to no pin.

    The logged branch is asserted because it is the whole of the rollout's
    evidence: `enforce`'s count says a pin did not arrive and never which of
    these branches sent it back empty. `legacy-path` is separate from `no-caps`
    on purpose -- below the era the transport carries no capabilities at all, so
    reporting it as a client that advertises none blames a setting for a
    protocol revision.
    """
    with caplog.at_level(logging.INFO, logger=scope.log.name):
        assert scope._ask(_Ctx(caps, version)) == ListRootsResult(roots=[])
    assert reason in caplog.text, caplog.text


def test_the_ask_is_made_when_the_client_can_answer_it():
    """The control. Without it the four above are satisfied by an `_ask` that
    never asks anyone, which is a pin that never arrives."""
    assert isinstance(scope._ask(_Ctx(_ROOTS, scope.MRTR)), ListRoots)


def test_the_log_names_the_client_and_whether_it_was_asked(caplog):
    """1768 zero-root pins were attributed to nobody. A count that cannot be
    split by client decides nothing, and the flag is waiting on that split."""
    verdict = scope._verdict(_Ctx(_ROOTS, scope.MRTR, client=("claude-code", "2.1.235")))
    with (
        caplog.at_level(logging.INFO, logger=scope.log.name),
        contextlib.suppress(scope.ScopeError),
    ):
        scope.enforce(Path("/nowhere"), ListRootsResult(roots=[]), verdict)
    # The third case the count conflated: asked, and answered with nothing.
    assert "client=claude-code/2.1.235" in caplog.text, caplog.text
    assert "branch=asked roots=0" in caplog.text, caplog.text


def test_the_verdict_reaches_enforce_from_a_real_resolver(caplog):
    """The defect this replaced a `ContextVar` for, driven the only way that can
    fail: through the framework's own resolver machinery.

    `call_tool` runs `_verdict` and `_ask` as the daemon does, so a sync
    resolver crosses `anyio.to_thread.run_sync` here -- which is where the
    ContextVar's write was discarded and `enforce` read `no resolver ran` while
    a root had arrived. A verdict handed in by the test cannot see that.

    The legacy branch, because the asked branch returns `ListRoots` and the
    round trip that answers it needs a client. That is the population the flag
    refuses, so it is also the one worth holding here.
    """
    ctx = Context(mcp_server=tools.mcp)
    ctx._request_context = SimpleNamespace(
        session=SimpleNamespace(client_capabilities=None, client_params=None),
        protocol_version="2025-06-18",
        meta={CLIENT_INFO_META_KEY: {"name": "probe", "version": "9"}},
        request=None,
    )
    with caplog.at_level(logging.INFO, logger=scope.log.name):
        out = anyio.run(tools.mcp.call_tool, "search", {"query": "x", "root": "/tmp"}, ctx)

    assert "client=probe/9 proto=2025-06-18 branch=legacy-path roots=0" in caplog.text, caplog.text
    assert "branch=direct" not in caplog.text, "the tool never received the resolved verdict"
    assert "sent no workspace roots" in out.structured_content["error"], out


def test_a_supplied_verdict_reaches_enforce_with_the_pin(two, pin, caplog):
    """The pinned half, which the resolver test above cannot reach in process."""
    mine, _ = two
    verdict = scope._verdict(_Ctx(_ROOTS, scope.MRTR, client=("claude-code", "2.1.235")))
    with caplog.at_level(logging.INFO, logger=scope.log.name):
        tools.search_code("handler", pin(mine), root=str(mine), mode="lexical", verdict=verdict)
    assert "branch=asked roots=1" in caplog.text, caplog.text
    assert "no resolver ran" not in caplog.text


def test_the_ledger_records_the_branch_under_the_isolated_state_dir(two, pin):
    """The count the flag turns on has to be groupable by client and survive a
    restart, which a seven-day journal is not. The path is resolved per call
    because a module constant kept the fleet's real ledger as the target of
    every test run -- the second assertion is that one."""
    mine, _ = two
    verdict = scope._verdict(_Ctx(_ROOTS, scope.MRTR, client=("claude-code", "2.1.235")))
    scope.enforce(mine, pin(mine), verdict)

    row = json.loads(pinledger.path().read_text().splitlines()[-1])
    assert (row["client"], row["branch"], row["roots"]) == ("claude-code/2.1.235", "asked", 1)
    assert pinledger.path().is_relative_to(config.STATE_DIR)
    assert not pinledger.path().is_relative_to(Path.home() / ".local"), "wrote to the real fleet"


def test_a_client_that_sends_no_info_is_logged_as_such(caplog):
    """`clientInfo` is optional on 2026-07-28+, so "unidentified" has to be a
    value in the population rather than a missing line."""
    with caplog.at_level(logging.INFO, logger=scope.log.name):
        scope._ask(_Ctx(None, scope.MRTR))
    assert "client=unidentified" in caplog.text, caplog.text
    assert "branch=no-caps" in caplog.text, caplog.text
