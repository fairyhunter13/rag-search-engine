"""The registry's two incident-bought rules, plus the late-join case."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coderag import config, quarantine, registry


def test_claim_then_join_a_root_keeps_one_row(tmp_path):
    """The late-join case: index a project, then a root symlinks it.

    One row, both claims recorded. A second row would mean a second index of
    the same code and two answers to "is this project excluded".
    """
    member = tmp_path / "member"
    member.mkdir()
    root = tmp_path / "root"
    root.mkdir()

    registry.claim(member, direct=True)
    registry.claim(member, root=root)

    rows = registry.load()
    assert len(rows) == 1
    entry = rows[str(member.resolve())]
    assert entry.direct is True
    assert entry.roots == [str(root.resolve())]


def test_a_root_release_spares_a_directly_claimed_member(tmp_path):
    """Both directions, because either alone passes with the logic inverted."""
    member = tmp_path / "member"
    member.mkdir()
    root = tmp_path / "root"
    root.mkdir()

    registry.claim(member, direct=True)
    registry.claim(member, root=root)

    assert registry.release(member, root=root) is False
    assert registry.get(member) is not None, "a directly claimed member must survive its root"

    assert registry.release(member, direct=True) is True
    assert registry.get(member) is None, "an unclaimed member must be dropped"


def test_two_roots_must_both_release_before_the_row_goes(tmp_path):
    member = tmp_path / "member"
    member.mkdir()
    for name in ("root_a", "root_b"):
        (tmp_path / name).mkdir()
        registry.claim(member, root=tmp_path / name)

    assert registry.release(member, root=tmp_path / "root_a") is False
    assert registry.release(member, root=tmp_path / "root_b") is True


def test_a_missing_path_is_never_pruned(tmp_path):
    """An unmounted volume looks exactly like a deleted project.

    The pruning version of load() wiped 236 rows when a caller ran it against
    the real state; removal is explicit or it does not happen.
    """
    gone = tmp_path / "was-here"
    gone.mkdir()
    registry.claim(gone, direct=True)
    key = str(gone.resolve())
    gone.rmdir()

    assert not Path(key).exists()
    assert key in registry.load(), "load() must not drop a row whose path is missing"


def test_concurrent_writers_do_not_lose_each_other(tmp_path, isolated_state):
    """The load-inside-the-lock rule, exercised across real processes.

    Reading before locking passes every single-threaded test and still loses
    rows here: each writer would start from the same snapshot and the last
    write would win. Eight subprocesses is enough to lose one reliably.
    """
    env = {**os.environ, "CODERAG_STATE_DIR": str(isolated_state)}
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys;from coderag import registry;registry.claim(sys.argv[1], direct=True)",
                str(tmp_path / f"p{i}"),
            ],
            env=env,
        )
        for i in range(8)
    ]
    for p in procs:
        assert p.wait(timeout=60) == 0

    written = json.loads((isolated_state / "projects.json").read_text())
    assert len(written) == 8, f"lost update: kept {len(written)} of 8 rows"


def test_backups_rotate_and_stay_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_KEEP", 3)
    for i in range(6):
        (tmp_path / f"p{i}").mkdir()
        registry.claim(tmp_path / f"p{i}", direct=True)

    kept = list(config.BACKUP_DIR.glob("projects.*.json"))
    assert 0 < len(kept) <= 3


def test_update_skips_a_row_that_was_unflagged_mid_index(tmp_path):
    """The indexer finishes work for projects the user already removed."""
    p = tmp_path / "p"
    p.mkdir()
    registry.update(p, chunk_count=99)
    assert registry.get(p) is None, "update() must not resurrect an unclaimed project"


def test_a_failure_that_resolves_itself_still_leaves_a_trace(tmp_path):
    """The whole point of the durable pair. `last_error` is cleared by the next success,
    and the hourly reconcile means that happens within the hour -- so a project could fail
    every sweep for a week and read clean at every moment anyone looked."""
    p = tmp_path / "p"
    p.mkdir()
    registry.claim(p, direct=True)

    registry.record_error(p, "embedding backend refused connection")
    failed = registry.get(p)
    assert failed.last_error == "embedding backend refused connection"
    assert failed.last_error_at is not None
    assert failed.error_total == 1

    registry.update(p, last_error=None)
    recovered = registry.get(p)
    assert recovered.last_error is None, "a success still clears the live error"
    assert recovered.last_error_at == failed.last_error_at, "when it broke must survive"
    assert recovered.error_total == 1, "that it broke must survive"


def test_the_error_total_accumulates_across_separate_failures(tmp_path):
    """A counter that assigned rather than incremented would report 1 forever, which reads
    as a single blip no matter how long the project has been failing."""
    p = tmp_path / "p"
    p.mkdir()
    registry.claim(p, direct=True)

    registry.record_error(p, "first")
    registry.record_error(p, "second")

    entry = registry.get(p)
    assert entry.error_total == 2
    assert entry.last_error == "second"


def test_record_error_skips_a_row_that_was_unflagged_mid_index(tmp_path):
    """Same contract as update(): finishing work for a project nobody claims must not
    recreate its row."""
    p = tmp_path / "p"
    p.mkdir()
    registry.record_error(p, "boom")
    assert registry.get(p) is None


def test_the_durable_error_fields_survive_a_round_trip_through_json(tmp_path):
    """They are only useful if they persist -- a field the registry drops on reload
    reports zero to every reader that opens the file fresh."""
    p = tmp_path / "p"
    p.mkdir()
    registry.claim(p, direct=True)
    registry.record_error(p, "boom")

    reloaded = registry.load()[str(registry.resolve(p))]
    assert reloaded.error_total == 1
    assert reloaded.last_error_at is not None


def test_record_error_is_the_only_thing_that_flags_a_failure():
    """A fifth call site that sets `last_error` itself leaves the two durable counters
    behind, and the row then reports a failure that never happened to the total. The
    existing four were found by grep, which finds nothing about the next one."""
    import ast

    writers = set()
    for path in sorted(Path(registry.__file__).parent.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef):
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.Assign):
                    continue
                if isinstance(node.value, ast.Constant) and node.value.value is None:
                    continue  # clearing on success stays every caller's to do
                if any(
                    isinstance(t, ast.Attribute) and t.attr == "last_error"
                    for t in node.targets
                ):
                    writers.add(f"{path.name}::{func.name}")

    assert writers == {"registry.py::record_error"}, (
        f"last_error is flagged outside record_error: {sorted(writers)}"
    )


def test_forget_removes_only_the_rows_it_was_handed(tmp_path):
    """No predicate and no disk read.

    Every directory here exists, so a `forget` that decided for itself which
    rows were dead would remove none -- and the version that did decide for
    itself removed 236.
    """
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
        registry.claim(tmp_path / name, direct=True)

    dropped, released = registry.forget([str(tmp_path / "b")])

    assert dropped == [str((tmp_path / "b").resolve())]
    assert released == []
    assert set(registry.load()) == {str((tmp_path / n).resolve()) for n in ("a", "c")}


def test_forget_strips_the_dead_root_from_the_members_that_survive_it(tmp_path):
    """The rule `release` applies, over a set: a member claimed only by the
    forgotten root goes with it, and one claimed directly stays with the dead
    root gone from its `roots` -- a stale root there narrows the member's corpus
    through the excludes it inherits, with the row count unmoved."""
    for name in ("root", "member", "shared"):
        (tmp_path / name).mkdir()
    root, member, shared = (tmp_path / n for n in ("root", "member", "shared"))
    registry.claim(root, direct=True)
    registry.claim(member, root=root)
    registry.claim(shared, direct=True, root=root)

    dropped, released = registry.forget([str(root)])

    assert dropped == [str(root.resolve())]
    assert released == [str(member.resolve())]
    assert registry.load()[str(shared.resolve())].roots == []


def test_forget_leaves_one_backup_holding_every_row_it_removed(tmp_path):
    """`_rotate_backup` stamps to the second, so a loop of `release` calls
    overwrites its own backup within that second and what survives is a
    half-pruned registry, which is not a restore point."""
    keys = []
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
        registry.claim(tmp_path / name, direct=True)
        keys.append(str((tmp_path / name).resolve()))

    registry.forget(keys)

    newest = max(config.BACKUP_DIR.glob("projects.*.json"))
    assert set(json.loads(newest.read_text())) == set(keys)
    assert registry.load() == {}


def _plant(tmp_path: Path, name: str) -> Path:
    """A registered project and the store directory that answers for it."""
    (tmp_path / name).mkdir()
    registry.claim(tmp_path / name, direct=True)
    store = config.index_path(tmp_path / name)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(b"x")
    return store.parent


def test_quarantine_is_not_counted_as_an_orphan(tmp_path):
    """`.trash/` lives under INDEX_DIR and no row names it.

    Counting it would have the reaper delete its own undo on the next pass, and
    report the deletion as reclaimed waste.
    """
    _plant(tmp_path, "live")
    quarantine.take(_plant(tmp_path, "dead"))
    registry.forget([str((tmp_path / "dead").resolve())])

    assert registry.unclaimed_stores() == []


def test_a_prune_against_an_empty_registry_refuses(tmp_path):
    """The shape both fleet wipes had. `force` deliberately does not lift it:
    a registry that failed to load looks exactly like a fleet with nothing
    enrolled, and a human forcing a prune is answering "delete these"."""
    _plant(tmp_path, "orphan")
    registry.forget([str((tmp_path / "orphan").resolve())])

    for force in (False, True):
        with (
            pytest.raises(RuntimeError, match="empty registry"),
            registry.prunable_stores(force=force),
        ):
            pass


def test_a_prune_over_half_the_tree_needs_force(tmp_path):
    """A verdict covering most of the tree is a claim about the input, not the
    disk. It is answerable -- with `--force` -- because a fleet really can be
    mostly stale; it is not answerable silently."""
    _plant(tmp_path, "keeper")
    for name in ("a", "b"):
        _plant(tmp_path, name)
        registry.forget([str((tmp_path / name).resolve())])

    with pytest.raises(RuntimeError, match="without --force"), registry.prunable_stores():
        pass
    with registry.prunable_stores(force=True) as (idle, busy):
        assert len(idle) + len(busy) == 2
