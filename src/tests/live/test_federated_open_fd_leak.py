"""FD1: a federated open that fails partway must not keep the stores it already opened.

`_search_sync` opens one sqlite store per federation member. That loop used to sit *above* the
`try:` whose `finally` closes them, so an exception raised mid-loop orphaned every store already
opened in that call — the list was local, nothing closed it, and under WAL each store costs ~3
fds held until the process exits.

That is not hypothetical. On 2026-07-29 one search over the 194-member root (157 of them with a
vector db) hit systemd's default `LimitNOFILE=1024`; each of three attempts leaked its partial
set, and the daemon ended holding 957 vectors.db fds with `/healthz` serving 500 because
`accept()` itself could no longer get an fd. The limit is now derived from the member count
(`daemon/systemd.py`), but a limit only moves the cliff — this gate is about what happens *at*
it. A failure to open must cost one query, not the daemon.

Discrimination, per [[feedback_guard_tests_must_discriminate]]: the tempting assertion — "the
call raises" — passes on the defect, since the defect raises too and merely leaks on the way out.
So this counts fds across the failure. The broken member is placed *last* on purpose: with it
first, nothing has been opened yet and both the fixed and the broken code leak nothing.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from rag_search.core.config import project_vector_db

pytestmark = pytest.mark.live


def _fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_fd1_failed_federated_open_releases_the_stores_already_opened(
    standalone_project_path, service_member_path, safe_tmp_path
):
    from rag_search.server.mcp import _search_sync

    broken = safe_tmp_path / "broken_member"
    broken.mkdir()
    (broken / "mod.py").write_text("def f():\n    return 1\n")
    vdb = project_vector_db(str(broken))
    vdb.parent.mkdir(parents=True, exist_ok=True)
    # Exists, so the loop opens it; not a database, so the open raises. sqlite reports
    # "file is not a database" from VectorStore.__init__, i.e. inside the opening loop.
    vdb.write_bytes(b"this is not a sqlite database" * 64)

    paths = [standalone_project_path, service_member_path, str(broken)]
    before = _fds()
    with pytest.raises(sqlite3.DatabaseError):
        _search_sync("anything", "code", paths, 5, "compact")
    leaked = _fds() - before

    # One store's worth is ~3 fds (db, -wal, -shm). The two good stores opened before the bad one
    # are what the defect stranded, so anything at or above a single store's cost means the
    # `finally` never saw them. The small allowance is for the failed store's own half-built
    # connection, which `pytest.raises` keeps reachable through the traceback until GC.
    assert leaked < 3, (
        f"FD1: {leaked} fds still held after a federated open failed partway. The store-opening "
        f"loop in _search_sync must run inside the try whose finally closes it, or one bad "
        f"member strands every store opened before it for the life of the process."
    )
