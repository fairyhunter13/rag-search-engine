"""FD1-FD4: a federated search's descriptor cost is bounded, and a failed open costs one query.

A federated search opens one sqlite store per member, and under WAL each store costs ~3 fds
(db, `-wal`, `-shm`). Two separate defects lived in the shape that opened them, and both bit on
2026-07-29 on the 194-member root (157 with a vector db):

- **Volume.** Every member was opened up front and held for the whole query — 471 descriptors for
  one question against systemd's default `LimitNOFILE=1024`, so a concurrent
  `overview(what="communities")` (a GraphStore per member) put it over the top.
- **The leak that made it permanent.** The opening loop sat *above* the `try:` whose `finally`
  closes the stores, so the EMFILE that volume produced orphaned every store already opened. The
  daemon ended holding 957 vectors.db fds with `/healthz` serving 500 because `accept()` itself
  could no longer get one, and *every* project's search failed with `unable to open database
  file` until it was restarted.

`search_federation` now takes db paths and owns the lifecycle, opening `batch` at a time under an
`ExitStack`. FD1 covers the leak, FD2/FD3 the volume, FD4 the ceiling behind both.

Discrimination, per [[feedback_guard_tests_must_discriminate]]: for FD1 the tempting assertion —
"the call raises" — passes on the defect, since the defect raises too and merely leaks on the way
out. So FD1 counts fds across the failure, and places the broken member *last* on purpose: with
it first, nothing has been opened yet and both the fixed and the broken code leak nothing.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest

from rag_search.core.config import project_vector_db

pytestmark = pytest.mark.live


def _fds() -> int:
    return len(os.listdir("/proc/self/fd"))


class _PeakFds:
    """Sample this process's open-fd count while a call runs; report the maximum seen.

    Sampling can only ever *under*-report, which is the safe direction for this gate: the defect
    it guards holds its whole descriptor set for the entire call, so no sampling rate can miss it,
    while a missed spike under the fix can only lower the measured peak. That asymmetry is why
    polling is sound here and not merely convenient.
    """

    def __init__(self, interval: float = 0.001):
        self._interval = interval
        self._stop = threading.Event()
        self.peak = 0

    def __enter__(self):
        self.peak = _fds()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            self.peak = max(self.peak, _fds())
            time.sleep(self._interval)

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(timeout=5)
        self.peak = max(self.peak, _fds())
        return False


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


# Enough members that a per-member peak and a per-batch peak are far apart: 40 stores is 120 fds
# held at once under the old shape against `_FANOUT_WORKERS * 3 == 24` under the new one. The
# bound sits between them with room for the poller's own sampling.
_MEMBERS = 40
_PEAK_BOUND = 48


def _dup_federation(path: str, n: int = _MEMBERS) -> list:
    """N db paths pointing at one real, indexed store.

    Repeating a path is how member *count* is varied without indexing 40 projects — the opens,
    the vec0 KNN and the FTS5 scan are all the real thing, which is what the property is about.
    Nothing here stands in for a component (cf. `test_no_mocks_or_fakes.py`); the store is real
    and the only synthetic part is how many times it appears in the input list.
    """
    return [project_vector_db(path)] * n


def test_fd2_federated_search_peak_is_bounded_by_batch_not_member_count(
    embedder, standalone_project_path
):
    """FD2: peak descriptors during a federated search track the batch width, not the federation.

    This is the volume half. `search_federation` holds `batch` stores at a time, so the peak is
    flat in member count — the only shape that survives a federation growing from 4 members to
    157 without a matching jump in the descriptor ceiling.
    """
    from rag_search.query.search import search_federation

    dbs = _dup_federation(standalone_project_path)
    with _PeakFds() as p:
        base = p.peak
        hits = search_federation("where is the ledger balance computed", embedder, dbs, top_k=5)
    delta = p.peak - base
    assert hits, "FD2 is vacuous: the search opened nothing, so there was no peak to bound"

    assert delta < _PEAK_BOUND, (
        f"FD2: {delta} fds held at peak across {_MEMBERS} members — that is per-member, not "
        f"per-batch. search_federation must open a batch at a time under an ExitStack, or peak "
        f"descriptor use grows with the federation until it reaches LimitNOFILE."
    )


def test_fd3_federated_search_returns_to_its_descriptor_baseline(
    embedder, standalone_project_path
):
    """FD3: every store a federated search opened is closed by the time it returns.

    FD1 covers the failure path; this covers the ordinary one. A batch that opened cleanly and
    was never closed leaks just as permanently as one abandoned by an exception, and would show
    up here as a delta proportional to the member count.
    """
    from rag_search.query.search import search_federation

    dbs = _dup_federation(standalone_project_path)
    before = _fds()
    hits = search_federation("where is the ledger balance computed", embedder, dbs, top_k=5)
    delta = _fds() - before
    assert hits, "FD3 is vacuous: the search opened nothing, so there was nothing to close"

    assert delta <= 2, (
        f"FD3: {delta} fds still held after a clean federated search over {_MEMBERS} members. "
        f"Store lifecycle must be scoped to the batch, not to the process."
    )


def test_fd4_unit_grants_descriptors_for_a_real_federation():
    """FD4: the unit raises LimitNOFILE off systemd's 1024 default.

    Defense in depth behind FD2 rather than a substitute for it — the GraphStore paths
    (`server/_overview.py`, `query/ask.py`) still open one connection per member for the length
    of a call, so the ceiling has to clear a real federation's worth on its own.
    """
    from rag_search.daemon.systemd import unit_text

    text = unit_text()
    assert "LimitNOFILE=" in text, (
        "FD4: unit_text() sets no LimitNOFILE, so the daemon inherits systemd's 1024 — within a "
        "factor of two of one federated query's descriptor cost."
    )
    limit = int(text.split("LimitNOFILE=")[1].split("\n")[0])
    assert limit >= 16384, f"FD4: LimitNOFILE={limit} is too close to the default 1024"
