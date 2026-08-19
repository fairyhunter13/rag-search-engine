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
    (root / config.PROJECT_CONFIG_NAME).write_text('[federation]\nexclude = ["links/m0"]\n')

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
    (root / config.PROJECT_CONFIG_NAME).write_text('[federation]\nexclude = ["*/_worktrees/*"]\n')

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
