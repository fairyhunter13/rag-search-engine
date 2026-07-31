"""RO1-RO6 + RC1-RC4: which projects a truncated reconcile walk actually reaches.

`reconcile_projects` returns on `is_paused()`. It now records where it stopped and resumes there
(RC1-RC4); before that it restarted at position 0 every pass and only ever completed the prefix,
which made the walk *order* decide which projects were reachable at all rather than merely the
order they were reached in — and the order was keyed on `last_change_seen`, which a never-indexed
project does not have.

RO4-RO6 cover the third key, added 2026-07-31: recency does not express *pipeline* drift. For a
graph re-derive every store already has vectors, so the `_has_vectors` key is constant over the
whole population and the sort collapses to recency — under which a stale store, being one nothing
has touched lately, lands at the tail. Measured then: 128 of 144 stores carried a superseded
`algo_version`, the 16 current ones sat at walk positions 59-74 and the stale ones at 58-201
(median 137), while no pass had reached past position 16 since the stamp last moved.

Measured on this host 07-30 after the registry wipe: 157 of 210 enabled rows held zero chunks, and
157 of 157 had an empty key, so `or ""` put every one of them at the tail of the `reverse=True`
walk. A live suite holds the pause lease for its entire run, which is how running the tests became
the thing preventing the rebuild those tests were waiting on.

RO2 is the arm that keeps this fix from undoing the previous one: recency ordering was itself
written for a starvation bug (a pass ground through 198 projects for 7.6 h without reaching either
repo edited that day), so it has to survive intact wherever the new key does not discriminate.

Subprocess-isolated with `RSE_INDEX_ROOT` redirected — `index_dir` resolves against a module-level
constant read at import, so only a fresh interpreter can point it somewhere the fleet is not.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_CHILD = r"""
import json, sqlite3, sys
from pathlib import Path
from rag_search.core.config import INDEX_ROOT, ProjectEntry, index_dir
from rag_search.daemon.sweeps import (
    _load_cursor, _pipeline_algo_version, _resume_at, _save_cursor, reconcile_order)

def row(name, seen, vectors, algo=None):
    p = f"/nonexistent/{name}"
    d = index_dir(p)
    if vectors:
        d.mkdir(parents=True, exist_ok=True)
        (d / "vectors.db").write_bytes(b"x")
    if algo is not None:
        # A real graph.db: the drift key reads the stored stamp, so a fake would prove nothing.
        d.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(d / "graph.db")
        con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT OR REPLACE INTO meta VALUES ('algo_version', ?)", (algo,))
        con.commit(); con.close()
    return ProjectEntry(path=p, enabled=True, last_change_seen=seen)

def names(rows):
    return [Path(e.path).name for e in reconcile_order(rows)]

# The real shape: the converged projects are also the recently-touched ones, because being indexed
# is what stamps them. So recency and need do not merely differ here, they point opposite ways.
out = {"mixed": names([
    row("fresh-indexed", "2026-07-30T22:00:00", True),
    row("older-indexed", "2026-07-29T10:00:00", True),
    row("never-a", "", False),
    row("never-b", None, False),
])}

# All indexed: the new key is constant, so the old recency ordering must come through untouched.
out["all_indexed"] = names([
    row("mid", "2026-07-29T10:00:00", True),
    row("newest", "2026-07-30T22:00:00", True),
    row("oldest", "2026-07-01T10:00:00", True),
])

# A store dir with no vectors.db is not an indexed project — an interrupted run leaves exactly this.
out["empty_store_dir"] = names([
    row("indexed", "2026-07-30T22:00:00", True),
    row("dir-but-no-vectors", "", False),
])
d = index_dir("/nonexistent/dir-but-no-vectors")
d.mkdir(parents=True, exist_ok=True)
out["empty_store_dir_after_mkdir"] = names([
    row("indexed", "2026-07-30T22:00:00", True),
    ProjectEntry(path="/nonexistent/dir-but-no-vectors", enabled=True, last_change_seen=""),
])

# The drift key. `cur` is the live stamp, so "current" tracks whatever the extractor is today.
cur = _pipeline_algo_version()
out["drift_beats_recency"] = names([
    row("current-newest", "2026-07-30T22:00:00", True, algo=cur),
    row("stale-oldest", "2026-07-01T10:00:00", True, algo="fg2+e2+2db7"),
])
out["never_beats_drift"] = names([
    row("stale-graph", "2026-07-30T22:00:00", True, algo="fg2+e2+2db7"),
    row("never-any", "", False),
])
out["recency_within_stale"] = names([
    row("s-mid", "2026-07-29T10:00:00", True, algo="fg2+e2+2db7"),
    row("s-new", "2026-07-30T22:00:00", True, algo="fg2+e2+2db7"),
    row("s-old", "2026-07-01T10:00:00", True, algo="fg2+e2+2db7"),
])

# The resume cursor. Plain entries — rotation is independent of how the walk was ordered.
walk = [ProjectEntry(path=f"/p/{c}", enabled=True, last_change_seen="") for c in "abcde"]
def rot(cursor):
    return [Path(e.path).name for e in _resume_at(walk, cursor)]
out["cursor_empty"] = rot("")
out["cursor_rotates"] = rot("/p/c")
out["cursor_unknown"] = rot("/p/zzz")
_save_cursor("/p/c"); out["cursor_roundtrip"] = _load_cursor()
_save_cursor(""); out["cursor_cleared"] = _load_cursor()
print(json.dumps(out))
"""


def _run(tmp: Path) -> dict:
    env = {**os.environ,
           "RSE_REGISTRY_PATH": str(tmp / "projects.json"),
           "RSE_INDEX_ROOT": str(tmp / "indexes"),
           "PYTHONPATH": str(Path(__file__).parents[2])}
    tmp.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([sys.executable, "-c", _CHILD],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, f"child exit {r.returncode}:\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def order() -> dict:
    """One child for every arm — they read one ordering, they do not each need their own.

    Module-scoped rather than using function-scoped `safe_tmp_path` because nothing here mutates
    what it reads. The dir still lives under the suite's own base, which is what
    `assert_under_test_base` and `test_no_real_project_in_tests.py` are written against.
    """
    import shutil

    from tests.live._projects import make_run_dir
    d = make_run_dir("ro-")
    try:
        yield _run(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_ro1_never_indexed_projects_are_reached_before_stale_ones(order):
    """RO1: both empty-key forms — "" and None — must precede every indexed project.

    Asserting a *prefix* rather than an exact list: the claim is which projects a truncated walk
    reaches, and that is a partition, not a permutation.
    """
    got = order["mixed"]

    assert set(got[:2]) == {"never-a", "never-b"}, (
        f"a project with no vectors is walked after ones that already have them: {got}")


def test_ro2_recency_order_survives_where_the_new_key_cannot_discriminate(order):
    """RO2: the ordering this change modifies was itself a starvation fix. Keep it."""
    got = order["all_indexed"]

    assert got == ["newest", "mid", "oldest"], (
        f"most-recently-touched-first was lost among equally-indexed projects: {got}")


def test_ro3_an_empty_store_dir_does_not_count_as_indexed(order):
    """RO3: `_has_vectors` must test the file, not the directory. An interrupted index leaves the
    dir behind, and treating that as done is how a project stays permanently unreachable — it sorts
    with the converged group and the walk never gets to it."""
    assert order["empty_store_dir"][0] == "dir-but-no-vectors", order["empty_store_dir"]
    assert order["empty_store_dir_after_mkdir"][0] == "dir-but-no-vectors", (
        f"an empty store dir was treated as an indexed project: "
        f"{order['empty_store_dir_after_mkdir']}")


def test_ro4_a_stale_stamp_outranks_a_newer_current_store(order):
    """RO4: the drift key's whole purpose. Recency alone puts the store the current extractor has
    never run on *behind* one it has, which is backwards — and measurably so: it is how 128 of 144
    stores sat past walk position 58 while no pass got beyond position 16."""
    assert order["drift_beats_recency"] == ["stale-oldest", "current-newest"], (
        f"a store on a superseded algo_version was walked after a current one: "
        f"{order['drift_beats_recency']}")


def test_ro5_never_embedded_still_outranks_a_drifted_graph(order):
    """RO5: the guard on RO1. A project with no vectors returns *nothing* for a search, while a
    drifted one returns real results from an older extractor — so drift slots in beneath the
    never-embedded key, not above it."""
    assert order["never_beats_drift"][0] == "never-any", order["never_beats_drift"]


def test_ro6_recency_survives_among_equally_drifted_stores(order):
    """RO6: RO2's argument again, one key lower. Where drift does not discriminate, the recency
    ordering it sits on top of has to come through intact."""
    assert order["recency_within_stale"] == ["s-new", "s-mid", "s-old"], (
        f"recency was lost among equally-stale stores: {order['recency_within_stale']}")


def test_rc1_a_cursor_resumes_the_walk_where_it_stopped(order):
    """RC1: the fix for restart-at-0. Rotation, not truncation — the prefix still gets walked, at
    the end of the lap, so resuming can never starve what it skipped past."""
    assert order["cursor_rotates"] == ["c", "d", "e", "a", "b"], order["cursor_rotates"]


def test_rc2_no_cursor_and_an_unknown_cursor_both_start_at_the_head(order):
    """RC2: a cleared cursor means "a lap finished, start at the priority head". An unknown one
    means the cursor's project left the registry — same answer, and it must not raise or, worse,
    return an empty walk, which would read as "nothing to reconcile"."""
    assert order["cursor_empty"] == ["a", "b", "c", "d", "e"], order["cursor_empty"]
    assert order["cursor_unknown"] == ["a", "b", "c", "d", "e"], order["cursor_unknown"]


def test_rc3_the_cursor_survives_the_process_that_wrote_it(order):
    """RC3: the cursor round-trips through the filesystem, not a module global. A daemon restart
    is precisely when the walk resets to 0, and `_PAUSED` is this repo's own standing lesson about
    a global that nothing can restore afterwards."""
    assert order["cursor_roundtrip"] == "/p/c", order["cursor_roundtrip"]


def test_rc4_a_completed_lap_clears_the_cursor(order):
    """RC4: the other half of RC1. A lap that ran to the end must drop back to the priority head,
    or the never-embedded and drifted projects at the front stay permanently mid-walk."""
    assert order["cursor_cleared"] == "", (
        f"a completed lap did not clear the cursor: {order['cursor_cleared']!r}")
