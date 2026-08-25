"""Layer 3, part three: the pin and federation together, over the wire.

Every other federation test pins at the root it already names, and every other
scope test runs below the daemon. So the one claim the design rests on, that a
member sits outside the workspace and is reached through the root anyway, was
carried by prose alone: a change making `expand` transitive and a change making
it pin-filtered break it in opposite directions and the suite stays green for
both.

The sixth amendment moved where the refusal comes from. A named root is now
admitted by its registry row, so these tests hold the registry gate to the job
the pin used to do, and they still hold the pin to expansion.

Chain under test, end to end: client roots -> workspace pin -> containment ->
registry resolve -> federation expand -> per-project stores -> one ranked list.
"""

from __future__ import annotations

import subprocess

import pytest

from live import Rpc, require_clear_gpu, require_daemon, until

pytestmark = pytest.mark.live

MEMBER_NEEDLE = "def reticulate_member_splines"
ROOT_NEEDLE = "def reticulate_root_splines"


def _repo(path, files: dict[str, str]):
    path.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


@pytest.fixture(scope="module")
def rpc():
    require_clear_gpu()
    client = Rpc(require_daemon())
    yield client
    client.close()


@pytest.fixture(scope="module")
def tree(tmp_path_factory):
    """A root, a member outside it reached by symlink, and a stranger.

    The member is built *outside* the root's directory on purpose: reached
    through the link, it is what a pinned call must still find, and named
    directly it is what a pinned call must still refuse.
    """
    base = tmp_path_factory.mktemp("scoping")
    member = _repo(base / "outside" / "member", {"src/m.py": f"{MEMBER_NEEDLE}():\n    return 1\n"})
    stranger = _repo(
        base / "outside" / "stranger", {"src/s.py": "def unrelated():\n    return 2\n"}
    )
    root = _repo(base / "root", {"src/r.py": f"{ROOT_NEEDLE}():\n    return 3\n"})
    (root / "member").symlink_to(member)
    return root, member, stranger


@pytest.fixture(scope="module")
def indexed(rpc, tree):
    """Index the root under a pin, and give every row back afterwards.

    The teardown runs on failure too: a row left enabled here is retried by
    reconcile and logged as a traceback at every daemon start, forever.
    """
    root, _, stranger = tree
    rpc.modern_tool("index", roots=[str(root)], root=str(root))
    rpc.tool("index", root=str(stranger))
    try:
        yield tree
    finally:
        rpc.tool("index", root=str(root), enabled=False)
        rpc.tool("index", root=str(stranger), enabled=False)


def _search(rpc, pinned_at, needle, **kw):
    """A pinned call: the roots go in the envelope, never in the arguments.

    `modern_tool` hands back the protocol envelope rather than the tool's own
    return value, so the unwrap belongs here and not in four assertions.
    """
    out = rpc.modern_tool("search", roots=[str(pinned_at)], query=needle, mode="lexical", **kw)
    return out["structuredContent"]


def test_1_a_pinned_search_reaches_the_member_outside_the_pin(indexed, rpc):
    """The whole chain in one call. `searched.projects == 2` is the half that
    says expansion happened; the member's own resolved path in a result is the
    half that says the store behind it is the member's, not the link's."""
    root, member, _ = indexed
    assert not member.is_relative_to(root), "the member must be outside for this to mean anything"

    # The member's own hit, not any hit: the root indexes first and answers
    # this query from its own file, so a predicate on `results` returns while
    # the member is still queued and the assertion below reads that as absent.
    until(
        lambda: [
            r
            for r in _search(rpc, root, MEMBER_NEEDLE).get("results", [])
            if r["project"] == str(member)
        ],
        timeout=300,
        what="the member's content to be searchable from its root",
    )

    full = _search(rpc, root, MEMBER_NEEDLE)
    assert full["searched"]["projects"] == 2, full["searched"]
    assert full["searched"]["files"] >= 2 and full["searched"]["chunks"] >= 2, full["searched"]


def test_2_the_roots_own_content_comes_back_from_the_same_pin(indexed, rpc):
    """The control: a pin that reached only the member would satisfy test 1."""
    root, _, _ = indexed
    out = until(
        lambda: _search(rpc, root, ROOT_NEEDLE).get("results"),
        timeout=300,
        what="the root's own content to be searchable",
    )
    assert any(r["project"] == str(root) for r in out), out


def test_3_a_project_outside_the_workspace_answers_once_it_is_indexed(indexed, rpc):
    """The boundary is the registry, not the pin. Registered, enabled and
    indexed, so the row itself is what admits this call. The pin was refusing it
    until the sixth amendment, and the caller then fell back to grep."""
    root, _, stranger = indexed
    out = until(
        lambda: _search(rpc, root, "unrelated", root=str(stranger)).get("results"),
        timeout=300,
        what="the stranger's content to be searchable by name",
    )
    assert all(r["project"] == str(stranger) for r in out), out


def test_4_a_path_no_row_holds_is_still_refused(indexed, rpc):
    """The discriminator for test 3. A server that answered every named root
    would pass it, so name a path the registry has never held: the refusal has
    to come from the registry gate and say so."""
    root, _, stranger = indexed
    out = _search(rpc, root, "unrelated", root=str(stranger.parent / "nobody"))
    assert "not indexed" in out.get("error", ""), out


def test_5_a_member_is_nameable_directly_now(indexed, rpc):
    """Reach was never the pin's to grant: the same pinned session already read
    this member through the root. The refusal is gone, so the name resolves.

    The member ranks first and the root still answers: `federation.unit` widens
    a member to its root's federation on purpose, because a member alone is
    1 project of 143 and the reply reads the same either way. So this asserts
    the member is reached, not that nothing else is.
    """
    root, member, _ = indexed
    out = until(
        lambda: _search(rpc, root, MEMBER_NEEDLE, root=str(member)).get("results"),
        timeout=300,
        what="the member to answer under its own name",
    )
    assert out[0]["project"] == str(member), out


def test_6_a_legacy_era_call_is_refused_now_that_the_flag_ships_on(indexed, rpc):
    """The rollout, landed: a pre-2026-07-28 client has nowhere to carry a pin,
    so it is refused with the message that says what to do -- and refused at
    the pin, not later at `is not indexed`, which the root's own index would
    otherwise answer."""
    root, _, _ = indexed
    out = rpc.call(
        "tools/call",
        {"name": "search", "arguments": {"query": ROOT_NEEDLE, "root": str(root)}},
    )["structuredContent"]
    assert "sent no workspace roots" in out.get("error", ""), out
