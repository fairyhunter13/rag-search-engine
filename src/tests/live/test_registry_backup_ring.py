"""RB1-RB4: the registry keeps a backup ring, and only deletions are allowed to consume it.

`projects.json` is written by `os.replace` with no history. On 2026-07-30 a test helper removed 198
rows in-process; ext4 has no snapshots and nothing had ever copied the file, so there was nothing to
roll back to and the fleet cost a full GPU re-index. `_registry_guard` covers the case where a live
test session is the one doing the deleting. This covers the rest: any writer, any process.

The ring is only worth having if it still holds the accident when you go looking, which is why RB1 is
the load-bearing test rather than RB2. A ring that rotates on every write is consumed by a single
daemon start — `register_all_members()` upserts once per discovered member, 193 on this host — so all
five copies would post-date any accident and the feature would look present while being useless.

Subprocess with `RSE_REGISTRY_PATH` redirected: it is read at import time, so this exercises the real
`_mutate`, the real flock and the real rotation against a real file, nowhere near the fleet's.
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
import json, sys
from pathlib import Path
from rag_search.core.config import ProjectEntry
from rag_search.core.registry import _load, remove_project, upsert_project

scenario, base = sys.argv[1], Path(sys.argv[2])
registry = Path(sys.argv[3])
paths = [str(base / ("p%d" % i)) for i in range(8)]
for p in paths:
    Path(p).mkdir(parents=True, exist_ok=True)
    upsert_project(ProjectEntry(path=p, enabled=True))

if scenario == "benign":
    for p in paths:                       # re-upsert every row: stamps, not membership changes
        upsert_project(ProjectEntry(path=p, enabled=True, chunk_count=41))
    for i in range(20):                   # and a burst of new rows, as federation discovery does
        q = str(base / ("extra%d" % i)); Path(q).mkdir(parents=True, exist_ok=True)
        upsert_project(ProjectEntry(path=q, enabled=True))
elif scenario == "one_deletion":
    remove_project(paths[0])
elif scenario == "wipe":
    for p in list(_load()):
        remove_project(p)
elif scenario == "spaced":                # 7 deletions, each beyond the cooldown from the last
    import os, time
    for p in paths[:7]:
        remove_project(p)
        newest = Path(str(registry) + ".bak.1")
        if newest.exists():               # age it past the gate instead of sleeping 10 minutes
            old = time.time() - 4000
            os.utime(newest, (old, old))

baks = sorted(p.name for p in registry.parent.glob(registry.name + ".bak.*"))
newest = json.loads(Path(str(registry) + ".bak.1").read_text()) if baks else {}
oldest_name = str(registry) + ".bak.%d" % len(baks)
oldest = json.loads(Path(oldest_name).read_text()) if baks else {}
print(json.dumps({
    "baks": baks,
    "newest_rows": len(newest),
    "oldest_rows": len(oldest),
    "live_rows": len(_load()),
    "first": paths[0],
}))
"""


def _run(tmp: Path, scenario: str) -> dict:
    registry = tmp / "projects.json"
    env = {**os.environ,
           "RSE_REGISTRY_PATH": str(registry),
           "RSE_INDEX_ROOT": str(tmp / "indexes"),
           "PYTHONPATH": str(Path(__file__).parents[2])}
    (tmp / "ws").mkdir(parents=True, exist_ok=True)
    r = subprocess.run([sys.executable, "-c", _CHILD, scenario, str(tmp / "ws"), str(registry)],
                       capture_output=True, text=True, env=env, timeout=180)
    assert r.returncode == 0, f"child exit {r.returncode}:\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_rb1_writes_that_add_or_restamp_rows_create_no_backups(safe_tmp_path):
    """RB1: 28 growing writes and 8 re-stamps leave the ring untouched.

    This is the arm that decides whether the ring is worth anything. The plan this replaced rotated
    inside `_mutate` unconditionally; measured against production that is ~193 rotations per daemon
    start, so the ring would always be full of the last few seconds of ordinary bookkeeping.
    """
    out = _run(safe_tmp_path / "rb1", "benign")
    assert out["baks"] == [], (
        f"ordinary writes burned the ring — an accident would already be rotated out: {out}")


def test_rb2_a_deletion_leaves_the_prior_state_recoverable(safe_tmp_path):
    """RB2: one removal writes one backup, and the backup still holds the removed row."""
    out = _run(safe_tmp_path / "rb2", "one_deletion")
    assert out["baks"] == ["projects.json.bak.1"], f"expected exactly one backup: {out}"
    assert out["newest_rows"] == 8 and out["live_rows"] == 7, (
        f"the backup does not predate the deletion: {out}")


def test_rb3_a_wipe_is_captured_once_at_the_state_it_started_from(safe_tmp_path):
    """RB3: the incident's exact shape — a removal loop — leaves one backup holding the intact set.

    The first version of this rotated per removal and this test is what refuted it: after 8
    sequential removals the *oldest* of the five copies had already lost 3 rows, so at the incident's
    198 rows every slot would have held a mid-wipe state and the ring would have been decoration.
    A burst is one event; the copy worth keeping is the one from before it started.
    """
    out = _run(safe_tmp_path / "rb3", "wipe")
    assert out["live_rows"] == 0, f"the wipe did not happen, so this proves nothing: {out}"
    assert out["baks"] == ["projects.json.bak.1"], (
        f"the burst rotated more than once and ate its own history: {out}")
    assert out["newest_rows"] == 8, (
        f"the one surviving copy is already mid-wipe — nothing here restores the fleet: {out}")


def test_rb4_deletions_far_enough_apart_each_get_a_slot_and_the_ring_stays_bounded(safe_tmp_path):
    """RB4: the cooldown suppresses a burst, it does not freeze the ring after the first rotation.

    Without this arm RB1–RB3 are all satisfied by a ring that rotates once and never again, which
    would be strictly worse than no ring: the copy you have is from whenever the file was first
    shrunk, and every later accident is invisible. Elapsed time is simulated by ageing the newest
    backup's mtime — the same clock the production gate reads, no code substituted for any of it.
    """
    out = _run(safe_tmp_path / "rb4", "spaced")
    assert out["baks"] == [f"projects.json.bak.{i}" for i in range(1, 6)], (
        f"7 spaced deletions did not fill and bound a 5-deep ring: {out}")
    assert out["newest_rows"] < out["oldest_rows"], (
        f"the slots are not ordered newest-first, so .bak.5 is not the furthest back: {out}")
