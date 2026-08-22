"""Discovery through symlinks, storage by resolved path."""

from __future__ import annotations

from pathlib import Path

from coderag import config, federation, registry


def _repo(path: Path) -> Path:
    (path / "src").mkdir(parents=True, exist_ok=True)
    (path / "src" / "a.py").write_text("x = 1\n")
    (path / ".git").mkdir(exist_ok=True)
    return path


def _tree(tmp_path, n=3):
    """A root whose `links/` holds symlinks to repos living elsewhere."""
    root = _repo(tmp_path / "root")
    (root / "links").mkdir()
    members = [_repo(tmp_path / "elsewhere" / f"m{i}") for i in range(n)]
    for i, m in enumerate(members):
        (root / "links" / f"m{i}").symlink_to(m)
    return root, members


def test_discovery_returns_resolved_targets_not_links(tmp_path):
    """The whole point: inotify does not traverse symlinks, so a link stored
    anywhere downstream is a member the watcher can never see change."""
    root, members = _tree(tmp_path)
    found = federation.discover(root)

    assert found == sorted(members)
    assert not any(p.is_symlink() for p in found)


def test_the_same_repo_linked_twice_is_one_member(tmp_path):
    """202 links collapse to ~135 repos on the live tree; without this every
    consumer does the work N times."""
    root, members = _tree(tmp_path, n=1)
    (root / "links" / "again").symlink_to(members[0])
    (root / "also").symlink_to(members[0])

    assert federation.discover(root) == [members[0]]


def test_the_collapse_holds_at_fleet_scale(tmp_path):
    """The 202-links-to-135-repos figure lives in the docstring above, measured
    on the live registry and asserted at n=1. Neither half is safe to assert
    directly: the registry names client paths, and one duplicate pair does not
    exercise a collapse.

    So the shape is reproduced instead -- many links, several repos reached
    twice, half of those through a link to a link -- and the assertion is the
    count, which is what a de-duplication working on one pair but not on a
    fan-out breaks.
    """
    root, members = _tree(tmp_path, n=135)
    for i in range(67):
        (root / "links" / f"dup{i}").symlink_to(root / "links" / f"m{i}")

    found = federation.discover(root)
    assert len(found) == 135
    assert found == sorted(members)
    assert not any(p.is_symlink() for p in found)


def test_a_link_pointing_back_inside_the_root_is_not_a_member(tmp_path):
    root, _ = _tree(tmp_path, n=0)
    (root / "self").symlink_to(root / "src")
    assert federation.discover(root) == []


def test_broken_links_and_cycles_are_skipped_not_fatal(tmp_path):
    root, members = _tree(tmp_path, n=1)
    (root / "links" / "dangling").symlink_to(tmp_path / "never-existed")
    (root / "loop").symlink_to(root / "loop")

    assert federation.discover(root) == [members[0]]


def test_federation_excludes_drop_a_member(tmp_path):
    root, members = _tree(tmp_path, n=2)
    (root / config.PROJECT_CONFIG_NAME).write_text("federation:\n  exclude: [links/m0]\n")

    found = federation.discover(root)
    assert members[0] not in found
    assert members[1] in found, "the exclude must be specific, not a blanket off-switch"


def test_a_federation_exclude_matches_where_the_link_points(tmp_path):
    """The live pattern is `*/_worktrees/*`, and it describes the *target*.

    59 links named `repositories/worktrees/<svc>` resolve into a sibling
    `_worktrees/` directory. Matching only the link's own path passes every
    other test in this file and silently re-enables all 59 -- 541,718 chunks,
    24.8% of what a federated query scanned, every one a second checkout.
    """
    root = _repo(tmp_path / "root")
    (root / "repositories" / "worktrees").mkdir(parents=True)
    wt = _repo(tmp_path / "elsewhere" / "_worktrees" / "svc")
    plain = _repo(tmp_path / "elsewhere" / "svc")
    (root / "repositories" / "worktrees" / "svc").symlink_to(wt)
    (root / "repositories" / "svc").symlink_to(plain)
    (root / config.PROJECT_CONFIG_NAME).write_text('federation:\n  exclude: ["*/_worktrees/*"]\n')

    found = federation.discover(root)
    assert wt not in found, "the exclude must match the resolved target"
    assert plain in found, "and must not swallow the real checkout beside it"


def test_register_claims_the_root_directly_and_members_by_root(tmp_path):
    root, members = _tree(tmp_path)
    assert federation.register(root) == members

    assert registry.get(root).direct is True
    for m in members:
        entry = registry.get(m)
        assert entry.direct is False
        assert entry.roots == [str(root)]
    assert federation.expand(root) == [root, *members]


def test_a_standalone_member_that_later_joins_keeps_one_row(tmp_path):
    """The late-join sequence, at the registry level: index it, then a root
    claims it. Two rows would mean two indexes of the same code."""
    root, members = _tree(tmp_path, n=1)
    member = members[0]
    registry.claim(member, direct=True)

    federation.register(root)

    entry = registry.get(member)
    assert entry.direct is True and entry.roots == [str(root)]
    assert len(registry.load()) == 2


def test_unregister_spares_a_member_that_was_also_claimed_directly(tmp_path):
    root, members = _tree(tmp_path, n=2)
    registry.claim(members[0], direct=True)
    federation.register(root)

    removed = federation.unregister(root)

    assert members[1] in removed and root in removed
    assert registry.get(members[0]) is not None
    assert registry.get(members[0]).roots == []


def test_unregister_releases_a_member_whose_link_is_already_gone(tmp_path):
    """Working off a fresh walk would strand exactly this row forever."""
    root, members = _tree(tmp_path, n=1)
    federation.register(root)
    (root / "links" / "m0").unlink()

    assert members[0] in federation.unregister(root)
    assert registry.load() == {}


def test_members_of_reads_the_registry_not_the_disk(tmp_path):
    """A link deleted mid-query must not silently shrink the corpus."""
    root, members = _tree(tmp_path, n=1)
    federation.register(root)
    (root / "links" / "m0").unlink()

    assert federation.members_of(root) == members


def test_a_disabled_member_leaves_the_search_unit(tmp_path):
    root, members = _tree(tmp_path, n=2)
    federation.register(root)
    registry.set_enabled(members[0], False)

    assert federation.expand(root) == [root, members[1]]


def test_discovery_is_depth_bounded(tmp_path):
    root, _ = _tree(tmp_path, n=0)
    deep = root / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    (deep / "link").symlink_to(_repo(tmp_path / "far"))

    assert federation.discover(root) == []


# ------------------------------------------------------------------ the sweep


def test_the_sweep_claims_a_link_added_after_the_last_index_call(tmp_path):
    """The gap the sweep exists to close. Discovery ran only inside `index`, so
    a repo symlinked into a root afterwards stayed invisible until someone
    remembered to re-run the tool -- or until the daemon restarted, which does
    not re-discover either."""
    root, members = _tree(tmp_path, n=1)
    federation.register(root)
    late = _repo(tmp_path / "elsewhere" / "late")
    (root / "links" / "late").symlink_to(late)

    assert registry.get(late) is None, "not a member before the sweep"
    claimed = federation.sweep()

    assert claimed == [registry.resolve(late)]
    assert registry.get(late).roots == [str(root)]
    assert registry.resolve(late) in federation.expand(root)


def test_the_sweep_is_idempotent(tmp_path):
    """It runs hourly forever. A second pass over an unchanged tree must claim
    nothing, or every member is re-submitted for a walk once an hour."""
    root, _ = _tree(tmp_path, n=2)
    federation.register(root)

    assert federation.sweep() == []


def test_the_sweep_never_releases_a_member_whose_link_vanished(tmp_path):
    """Removal is explicit. An unmounted volume and a deleted link are the same
    observation from here, and the pruning version of this rule wiped the fleet
    registry once already."""
    root, members = _tree(tmp_path, n=2)
    federation.register(root)
    (root / "links" / "m0").unlink()

    federation.sweep()

    assert registry.get(members[0]) is not None
    assert registry.get(members[0]).roots == [str(root)]


def test_one_unparseable_config_does_not_stop_the_sweep(tmp_path):
    """A typo in one repo used to take the whole watch thread down. The sweep
    walks every root, so it has the same exposure and must not share the fate."""
    broken, _ = _tree(tmp_path, n=0)
    (broken / ".coderag.yaml").write_text("index: [not, a, mapping]\n")
    registry.claim(broken, direct=True)
    healthy, _ = _tree(tmp_path / "other", n=1)
    federation.register(healthy)
    late = _repo(tmp_path / "other" / "elsewhere" / "late")
    (healthy / "links" / "late").symlink_to(late)

    claimed = federation.sweep()

    assert registry.resolve(late) in claimed, "a broken sibling blocked the sweep"
    assert registry.get(broken).last_error


def test_the_sweep_skips_a_member_it_would_otherwise_treat_as_a_root(tmp_path):
    """Only direct rows are walked. A member is reachable through its root and
    walking it too would federate one level deeper than the design allows."""
    root, members = _tree(tmp_path, n=1)
    federation.register(root)
    (members[0] / "links").mkdir()
    deeper = _repo(tmp_path / "elsewhere" / "deeper")
    (members[0] / "links" / "deeper").symlink_to(deeper)

    assert federation.sweep() == []
    assert registry.get(deeper) is None
