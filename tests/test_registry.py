"""The registry's two incident-bought rules, plus the late-join case."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from coderag import config, registry


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
