"""Layer 3, part one: federation against the running daemon.

Real repos on disk, real symlinks, real inotify, real GPU, one daemon. The
fixtures build the tree in a tmp dir rather than pointing at the fleet, because
every assertion below needs to know what the answer *should* be -- an exclude
test against a repo whose contents you did not write is satisfied by an empty
index. The fleet root is measured separately, where the question is latency and
member count rather than correctness.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from coderag import config
from live import Rpc, require_clear_gpu, require_daemon, until

pytestmark = pytest.mark.live

# Not `vendor/bundle.min.js`, which is what this was. `vendor/*` and `*.min.js`
# are both in `DEFAULT_IGNORES`, so no root's exclude was ever what dropped it:
# test 4 asserted a mechanism that could not have run, and tests 5 and 6 waited
# 300 s each for a file the indexer is never allowed to see. The path has to be
# one the *config* excludes and the defaults do not, or the assertion is about
# `DEFAULT_IGNORES` under another name.
VENDORED = "thirdparty/bundle.js"
FIRST_PARTY = "src/session.py"
NEEDLE = "def rotate_session_secret"


def test_0_the_excluded_path_is_one_the_defaults_would_have_indexed():
    """The fixture's own precondition, asserted rather than assumed.

    Every exclude assertion in this file is satisfied by a file that never got
    indexed, whatever the reason -- so if `DEFAULT_IGNORES` grows a pattern that
    covers `VENDORED`, four tests go on passing while the mechanism they name
    stops being exercised. This is the only place that can notice.
    """
    from coderag import discover, projcfg

    assert discover.indexable(VENDORED, projcfg.ProjectConfig()), (
        f"{VENDORED} is dropped before any config exclude sees it"
    )


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
def member(tmp_path_factory):
    """A repo with one first-party file and one vendored blob, so both
    directions of an exclude assertion have something to land on."""
    return _repo(
        tmp_path_factory.mktemp("member"),
        {
            FIRST_PARTY: f"{NEEDLE}(store):\n    return store.rotate()\n",
            VENDORED: "function ck(){return 'vendored editor bundle'}\n",
        },
    )


@pytest.fixture(scope="module")
def root(tmp_path_factory, member):
    """A root that reaches the member only through a directory symlink."""
    path = _repo(tmp_path_factory.mktemp("root"), {"app.py": "import member\n"})
    (path / config.PROJECT_CONFIG_NAME).write_text('index:\n  exclude: ["thirdparty/*"]\n')
    (path / "linked-member").symlink_to(member, target_is_directory=True)
    return path


def _hits(rpc, query, root, **kw):
    out = rpc.tool("search", query=query, root=str(root), mode="lexical", **kw)
    assert "error" not in out, out
    return [r["path"] for r in out["results"]]


def _indexed(rpc, root):
    return until(
        lambda: (rpc.tool("index", root=str(root))["indexed"]["chunks"] or 0) > 0,
        timeout=180,
        what="the root's own chunks to land",
    )


# ---------------------------------------------------------------- the sequence


def test_1_index_returns_before_the_work_does(rpc, root):
    """A return that blocks is a failure even if the index is eventually
    correct: the caller is an agent mid-answer, not a build script."""
    started = time.perf_counter()
    out = rpc.tool("index", root=str(root))
    assert time.perf_counter() - started < 1.0, "index blocked on the build"
    assert out["members"] == 1, out
    # `watching` is per-project now and the rebuild is asynchronous, so it is
    # false here by design -- the arming assertion belongs where something
    # waits for it.
    assert until(
        lambda: rpc.tool("index", root=str(root))["watching"],
        timeout=60,
        what="the root's watches to be armed",
    )


def test_2_the_member_is_registered_under_its_real_path(rpc, root, member):
    """The symlink is a discovery mechanism and must not survive into state --
    inotify does not traverse one, so a stored symlink is a watcher that
    silently never fires."""
    out = rpc.tool("index", root=str(root))
    assert out["members"] == 1
    # The row, not a search that happens to answer: `index` returns before the
    # member's pass runs, so the old proxy -- "it does not error as unregistered"
    # -- now hits the tightened gate for a few seconds. Naming the member by its
    # real path is the assertion; had the symlink been stored, this would be a
    # fresh row carrying no root at all.
    assert str(root) in rpc.tool("index", root=str(member))["roots"]


def test_3_search_from_the_root_reaches_the_member(rpc, root):
    _indexed(rpc, root)
    paths = until(
        lambda: _hits(rpc, NEEDLE, root),
        timeout=300,
        what="the member's content to become searchable from the root",
    )
    assert any(FIRST_PARTY in p for p in paths), paths


def test_4_the_roots_excludes_reach_the_member_that_has_no_config(rpc, root):
    """Both directions, or an empty index passes the half that matters."""
    assert _hits(rpc, NEEDLE, root), "first-party content must be findable"
    assert not [p for p in _hits(rpc, "vendored editor bundle", root) if VENDORED in p]


def test_5_a_late_join_narrows_an_existing_index_without_a_rebuild(rpc, member, root):
    """The real sequence: standalone first, root second. A test that flags the
    root first never exercises the reconcile at all."""
    rpc.tool("index", root=str(root), enabled=False)
    rpc.tool("index", root=str(member))
    _indexed(rpc, member)
    assert until(
        lambda: [p for p in _hits(rpc, "vendored editor bundle", member) if VENDORED in p],
        timeout=300,
        what="the standalone member to index its vendored blob",
    )

    rpc.tool("index", root=str(root))
    assert until(
        lambda: not [p for p in _hits(rpc, "vendored editor bundle", member) if VENDORED in p],
        timeout=300,
        what="the root's excludes to remove the vendored chunks",
    )
    status = rpc.tool("index", root=str(member))
    assert str(root) in status["roots"], status
    assert _hits(rpc, NEEDLE, member), "narrowing must not cost the first-party file"


def test_6_unflagging_the_root_widens_the_member_back(rpc, member, root):
    rpc.tool("index", root=str(root), enabled=False)
    assert until(
        lambda: [p for p in _hits(rpc, "vendored editor bundle", member) if VENDORED in p],
        timeout=300,
        what="the widened config to re-add what the root was suppressing",
    )
    assert rpc.tool("index", root=str(member))["roots"] == []


def test_7_a_typo_in_the_config_is_an_error_that_names_the_nearest_key(rpc, tmp_path_factory):
    """A silently ignored exclude typo is how an index ends up three times too
    large, so the failure has to be loud and at the call that caused it."""
    project = _repo(tmp_path_factory.mktemp("typo"), {"a.py": "x = 1\n"})
    (project / config.PROJECT_CONFIG_NAME).write_text('index:\n  excludes: ["wiki/*"]\n')
    out = rpc.tool("index", root=str(project))
    error = until(
        lambda: out.get("last_error") or rpc.tool("index", root=str(project)).get("last_error"),
        timeout=60,
        what="the config error to surface on the project's status",
    )
    assert "excludes" in error and "exclude" in error, error


def test_8_teardown_leaves_the_fleet_alone(rpc, root, member):
    """Not a courtesy: this suite runs against the real registry, and a live
    test that pruned rows is what destroyed it once already."""
    for path in (root, member):
        rpc.tool("index", root=str(path), enabled=False)
    assert rpc.tool("index", root=str(root), enabled=False)["members_released"] == []
