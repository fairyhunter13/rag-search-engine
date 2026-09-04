"""Automatic removal, and the four ways it must refuse.

Every arm here is a real directory removed from a real filesystem, against the
isolated registry `conftest` already redirects. The clock is the one injected
thing, because a test must not spend the grace period.

The rule this replaces refused to prune at all, so each refusal arm below is the
one that keeps the replacement honest.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from coderag import config, disk, federation, prune, quarantine, registry


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
    assert pruner.run_due() == {"forgotten": [], "unclaimed": [], "quarantined": []}
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
    assert pruner.run_due() == {"forgotten": [], "unclaimed": [], "quarantined": []}


def _plant_store(project: Path, *, age: float = 0.0) -> Path:
    """A store directory where the reaper will look for this project's."""
    store = config.index_path(project)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(b"x" * 64)
    if age:
        stamp = time.time() - age
        os.utime(store, (stamp, stamp))
    return store.parent


def test_a_dead_row_takes_its_store_with_it(make, pruner, clock):
    """The whole leak, in one arm: the row left and the bytes stayed.

    197 stores on this fleet, 748 MB, every one of them a row the watcher had
    already removed. Nothing returned a byte without a human typing a command.
    """
    project = make("doomed")
    registry.claim(project, direct=True)
    store_dir = _plant_store(project, age=config.PRUNE_MIN_IDLE_S + 60)
    shutil.rmtree(project)

    pruner.note_gone(project)
    clock.advance(31.0)
    verdict = pruner.run_due()

    assert verdict["quarantined"] == [str(project)]
    assert not store_dir.exists()
    # Moved, not deleted: both wipes surfaced days later, when someone searched
    # a repo and got nothing back.
    trashed = list(quarantine.trash_dir().iterdir())
    assert [p.name.split("-", 1)[1] for p in trashed] == [store_dir.name]


def test_a_store_written_inside_the_idle_floor_is_left_alone(make, pruner, clock):
    """`prune-raced-a-store-the-daemon-was-writing`, on the automatic path.

    A job queued before its row was dropped still indexes into the directory,
    with no row to hold it. That defect was recorded against the hand-typed
    prune; moving the removal onto the delete event is when it recurs.
    """
    project = make("busy")
    registry.claim(project, direct=True)
    store_dir = _plant_store(project)
    shutil.rmtree(project)

    pruner.note_gone(project)
    clock.advance(31.0)
    verdict = pruner.run_due()

    assert verdict["forgotten"] == [str(project)]
    assert verdict["quarantined"] == []
    assert store_dir.is_dir()


def test_a_failed_quarantine_never_degrades_to_a_delete(make, pruner, clock, monkeypatch):
    """A path whose purpose is undo cannot answer a failure by deleting harder."""
    project = make("unmovable")
    registry.claim(project, direct=True)
    store_dir = _plant_store(project, age=config.PRUNE_MIN_IDLE_S + 60)
    shutil.rmtree(project)
    monkeypatch.setattr(
        Path, "rename", lambda *_a, **_k: (_ for _ in ()).throw(OSError("cross-device"))
    )

    pruner.note_gone(project)
    clock.advance(31.0)
    verdict = pruner.run_due()

    assert verdict["quarantined"] == [], "a store it could not move was reported as removed"
    assert store_dir.is_dir()


def test_quarantine_expires_on_its_own_clock(make):
    """Seven days of undo, and a name it did not write is left where it stands."""
    project = make("expiring")
    store_dir = _plant_store(project)
    moved = quarantine.take(store_dir)
    stranger = quarantine.trash_dir() / "not-ours"
    stranger.mkdir()

    assert quarantine.expire() == []
    gone = quarantine.expire(now=time.time() + config.QUARANTINE_DAYS * 86400 + 1)

    assert gone == [moved]
    assert stranger.is_dir()


def test_an_unmounted_volume_is_never_read_as_a_deletion(make, monkeypatch):
    """The cold-start reconciliation answers four ways, and only one is actionable.

    A mount point left standing after its volume goes away is an empty
    directory whose parent exists -- the exact shape of a deleted repo. The
    recorded st_dev is the only thing that separates them.
    """
    project = make("on-a-volume")
    entry = registry.claim(project, direct=True)
    assert entry.dev, "the claim recorded no device to compare against"
    shutil.rmtree(project)

    assert prune.survey()["deleted"] == [str(project)]

    monkeypatch.setattr(disk, "device", lambda _p: entry.dev + 1)
    registry.claim(make("on-a-volume"), direct=True)
    shutil.rmtree(project)
    moved = registry.load()[str(project)]

    assert prune.verdict(moved) == "unmounted"
    after = prune.survey()
    assert after["deleted"] == []
    # The population, not a total of the three lists: an empty `deleted` and a
    # registry that read nothing are otherwise the same answer.
    assert after["read"] == len(registry.load()) == 1


def test_a_delete_event_reaches_the_queue(make, monkeypatch):
    """The wiring between the watcher and the reaper, which had no test at all.

    Every arm above drives `note_gone` by hand. That covers the reaper and
    proves nothing about the half that decides *which* of a 4,000-path batch is
    a deletion -- the half that makes the whole feature dead code if it is wrong.
    """
    doomed = make("doomed")
    survivor = make("survivor")
    registry.claim(doomed, direct=True)
    registry.claim(survivor, direct=True)
    monkeypatch.setattr(prune, "PRUNER", prune.Pruner(grace=30.0, clock=Clock()))
    prune.register_paths([doomed, survivor])
    shutil.rmtree(doomed)

    # watchfiles yields `(Change, str)`, and the path is a plain string.
    noted = prune.note_deletions({(3, str(doomed)), (3, str(survivor))})

    assert noted == 1, "the surviving project was queued, or the dead one was not"
    assert prune.PRUNER.depth == 1


def test_an_unregistered_path_is_not_a_deletion(make, monkeypatch):
    """A batch is every write under every watched root, not a list of projects.

    `_keys` is the registry, so a file removed inside a live project reaches
    this on every `git checkout` and must not queue its project for removal.
    """
    project = make("live")
    registry.claim(project, direct=True)
    monkeypatch.setattr(prune, "PRUNER", prune.Pruner(grace=30.0, clock=Clock()))
    prune.register_paths([project])
    (project / "a.py").unlink()

    assert prune.note_deletions({(3, str(project / "a.py"))}) == 0
    assert prune.PRUNER.depth == 0


def test_a_row_that_never_recorded_a_device_gets_one_back(make):
    """The backfill, and the verdict it unblocks.

    A row enrolled before `dev` existed answers `unknown` forever, and `unknown`
    is the verdict `--prune` never acts on. 136 of 503 rows on the fleet this
    was written against were in that state, and each one was one deletion away
    from paging every hour with no command able to remove it.

    The two halves are asserted apart on purpose: the field getting a value is
    not the fix, and the verdict moving off `unknown` is. The control for the
    second half is `test_an_empty_directory_is_left_blind`, where the same
    removal against an unfilled row still answers `unknown`.
    """
    project = make("enrolled-before-the-field")
    registry.claim(project, direct=True)
    registry.update(project, dev=0)
    key = str(project)
    assert registry.load()[key].dev == 0

    assert registry.record_devices() == [key]
    assert registry.load()[key].dev == project.stat().st_dev

    shutil.rmtree(project)
    assert prune.verdict(registry.load()[key]) == "deleted"
    assert prune.survey()["deleted"] == [key]


def test_an_empty_directory_is_left_blind(make, tmp_path):
    """A bare mount point must not be given the underlay's device.

    An unmounted volume leaves its mount point standing as an *empty*
    directory. To record the device that answers it would have a later verdict
    call an intact repository `deleted` -- the predicate that wiped the fleet,
    reached by backfill rather than by scan.

    One occupied row stands beside the empty one, because a backfill that
    filled nothing at all passes the second assertion for the wrong reason.
    """
    live = make("occupied")
    registry.claim(live, direct=True)
    registry.update(live, dev=0)

    mount_point = tmp_path / "projects" / "the-volume-is-away"
    mount_point.mkdir(parents=True)
    registry.claim(mount_point, direct=True)
    registry.update(mount_point, dev=0)

    filled = registry.record_devices()

    assert filled == [str(live)]
    assert registry.load()[str(mount_point)].dev == 0

    # The row stays blind, so the removal below reads `unknown` and not
    # `deleted`. That is the price of the guard, and `doctor` prints the
    # `forget` command rather than leaving the row with no way out.
    mount_point.rmdir()
    assert prune.verdict(registry.load()[str(mount_point)]) == "unknown"


def test_a_sweep_with_nothing_to_fill_writes_nothing(make):
    """What the hourly call costs on a fleet that is already complete.

    `_mutate` rotates a backup on every exit, so an unconditional write here
    spends the whole `BACKUP_KEEP` window on no-ops and drops the backups that
    exist to restore the registry.
    """
    registry.claim(make("already-recorded"), direct=True)
    assert registry.record_devices() == []

    before = sorted(p.name for p in config.BACKUP_DIR.glob("projects.*.json"))
    assert registry.record_devices() == []
    assert sorted(p.name for p in config.BACKUP_DIR.glob("projects.*.json")) == before


def test_a_deleted_member_link_queues_the_root_that_held_it(make, monkeypatch):
    """The other arm: a link is gone, its target may be entirely alive.

    So the root is queued, never the member -- `note_unlinked`, which releases
    one claim, and not `note_gone`, which retires a store.
    """
    root = make("workspace")
    member = make("member")
    (root / "repositories").mkdir()
    link = root / "repositories" / "member"
    link.symlink_to(member)
    registry.claim(root, direct=True)
    federation.register(root)
    monkeypatch.setattr(prune, "PRUNER", prune.Pruner(grace=30.0, clock=Clock()))
    prune.register_paths([root])
    link.unlink()

    assert prune.note_deletions({(3, str(link))}) == 1
    assert prune.PRUNER.depth == 1
    assert registry.get(member) is not None, "the member's own row must survive its link"
