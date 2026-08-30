"""Automatic removal, and the four ways it must refuse.

Every arm here is a real directory removed from a real filesystem, against the
isolated registry `conftest` already redirects. The clock is the one injected
thing, because a test must not spend the grace period.

The rule this replaces refused to prune at all, so each refusal arm below is the
one that keeps the replacement honest.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from coderag import federation, prune, registry


@pytest.fixture
def make(tmp_path):
    """A directory that `_looks_like_a_project` accepts, at any relative path."""

    def build(name: str) -> Path:
        root = tmp_path / "projects" / name
        root.mkdir(parents=True)
        (root / "a.py").write_text("def alpha():\n    return 1\n")
        return root

    return build


class Clock:
    """A hand-wound monotonic clock. `advance` is the whole grace period."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def pruner(clock):
    return prune.Pruner(grace=30.0, clock=clock)


def test_a_deleted_project_loses_its_row(make, pruner, clock):
    """The ruling: missing or dead is removed, with no command run."""
    project = make("gone")
    registry.claim(project, direct=True)
    shutil.rmtree(project)

    pruner.note_gone(project)
    assert pruner.run_due() == {"forgotten": [], "unclaimed": []}
    assert registry.get(project) is not None

    clock.advance(31.0)
    assert pruner.run_due()["forgotten"] == [str(project)]
    assert registry.get(project) is None


def test_a_removed_parent_keeps_the_row(make, pruner, clock):
    """The unmount case, and the reason the old rule refused every prune.

    A repo deleted leaves its parent standing. A volume that goes away takes the
    parent with it, and the two are told apart by exactly this test.
    """
    project = make("volume/inner")
    registry.claim(project, direct=True)
    shutil.rmtree(project.parent)

    pruner.note_gone(project)
    clock.advance(31.0)
    assert pruner.run_due()["forgotten"] == []
    assert registry.get(project) is not None


def test_a_project_restored_inside_the_grace_period_keeps_the_row(make, pruner, clock):
    """A checkout into a moved-aside path settles well inside the window."""
    project = make("blinking")
    registry.claim(project, direct=True)
    shutil.rmtree(project)

    pruner.note_gone(project)
    clock.advance(10.0)
    project.mkdir()
    (project / "a.py").write_text("def alpha():\n    return 1\n")

    clock.advance(21.0)
    assert pruner.run_due()["forgotten"] == []
    assert registry.get(project) is not None


def test_a_removed_link_releases_the_claim_and_the_target_survives(make, pruner, clock):
    """The second path. No event ever fires on the target, which still exists."""
    member = make("member")
    root = make("root")
    links = root / "repositories"
    links.mkdir()
    (links / "member").symlink_to(member)

    federation.register(root)
    assert registry.get(member) is not None
    (links / "member").unlink()

    pruner.note_unlinked(root)
    clock.advance(31.0)
    verdict = pruner.run_due()
    assert verdict["unclaimed"] == [str(member)]
    assert verdict["forgotten"] == [str(member)]
    assert registry.get(member) is None
    # The link went. The project did not, and nothing here removed it.
    assert member.is_dir()


def test_a_removed_link_keeps_a_row_another_root_claims(make, pruner, clock):
    """`release` drops one claim. It is not the prune that emptied a fleet."""
    member = make("shared")
    first = make("first")
    second = make("second")
    for root in (first, second):
        (root / "repositories").mkdir()
        (root / "repositories" / "shared").symlink_to(member)
        federation.register(root)

    entry = registry.get(member)
    assert entry is not None
    assert sorted(entry.roots) == sorted([str(first), str(second)])

    (first / "repositories" / "shared").unlink()
    pruner.note_unlinked(first)
    clock.advance(31.0)
    verdict = pruner.run_due()
    assert verdict["unclaimed"] == [str(member)]
    assert verdict["forgotten"] == []

    entry = registry.get(member)
    assert entry is not None
    assert entry.roots == [str(second)]


def test_a_directly_enrolled_member_survives_its_link(make, pruner, clock):
    """Enrolled on its own account, so no root's claim can remove it."""
    member = make("own")
    root = make("owner")
    (root / "repositories").mkdir()
    (root / "repositories" / "own").symlink_to(member)
    federation.register(root)
    registry.claim(member, direct=True)

    (root / "repositories" / "own").unlink()
    pruner.note_unlinked(root)
    clock.advance(31.0)
    pruner.run_due()
    assert registry.get(member) is not None


def test_nothing_due_reads_no_registry(pruner):
    """Called on every watcher tick, so the empty case must cost nothing."""
    assert pruner.depth == 0
    assert pruner.run_due() == {"forgotten": [], "unclaimed": []}
