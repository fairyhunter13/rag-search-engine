"""The fleet walk `doctor --compact` does, and the descriptors it must not hold.

The walk opened every store and closed none. Nothing on this machine noticed:
an interactive shell allows 1048576 open files, and 423 WAL stores at three
descriptors each is 1269. The first run under the weekly timer failed at once,
because a systemd service gets a soft NOFILE of 1024 and no warning that it
differs from the shell that tested the code.
"""

from __future__ import annotations

import struct

import pytest

from coderag import config, conns, doctor, registry, store


def _reset() -> None:
    conns.close_all()
    with conns._caches_lock:
        conns._caches.clear()
    conns._local.__dict__.pop("cache", None)


@pytest.fixture
def three_stores(tmp_path):
    """Three enrolled projects, each with a store on disk and one live vector."""
    one = [0.01] * config.EMBED_DIMS
    for name in ("alpha", "beta", "gamma"):
        project = tmp_path / name
        project.mkdir()
        (project / "a.py").write_text("def alpha():\n    return 1\n")
        conn = store.connect(project)
        with conn:
            conn.execute(
                "INSERT INTO chunks_vec(chunk_id, embedding) VALUES(?, ?)",
                (1, struct.pack(f"{config.EMBED_DIMS}f", *one)),
            )
        registry.claim(project, direct=True)
    _reset()


def test_the_walk_holds_one_store_open_at_a_time(three_stores, capsys):
    """The arm the old code fails: it ended with three handles open, and a fleet
    walk ends with 423. The count is asserted exactly, because a bound would
    have passed against the code that leaked."""
    assert doctor._compact() == 0

    assert conns.open_count() == 0
    assert "skipped 3 store(s) already packed" in capsys.readouterr().out


def test_the_walk_records_what_it_saw(three_stores):
    """The weekly interval is a guess until two of these rows exist. Their
    difference is how many blocks the fleet regrew in between, so a pass that
    reports nothing leaves the interval unmeasurable forever."""
    from coderag import runledger

    doctor._compact()

    row = runledger.read(limit=1, kind="compact")[0]
    assert row["walked"] == 3
    assert row["skipped"] == 3
    assert row["blocks_before"] == 0
