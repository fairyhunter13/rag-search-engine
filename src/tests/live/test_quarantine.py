"""QT1-QT5: a deleted store is recoverable for a week, and nothing quietly re-collects it.

R1's floor makes a wrong sweep loud, but it can only recognise "this answer is not credible" — a
smaller, entirely credible number of stores can be just as wrong. Quarantine covers the other half:
a registry row is re-derivable from a federation walk in milliseconds, the embeddings under a store
are GPU minutes per project and were hours for the fleet, so the expensive half gets the undo.

What is guarded is not the deletion but a quarantine that turns out not to be one — one way per skip
site, and QT4's has no `--yes` in front of it: `purge_unowned_index_dirs_created_since` deletes on
"appeared since the snapshot" and "no registry row owns it", both true of a `.trash` created mid-run.

Subprocess-isolated with `RSE_INDEX_ROOT` and `RSE_REGISTRY_PATH` redirected — both are read at
import time, so the child drives the real functions with the fleet unreachable from them.
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
import json, os, sys, time
from pathlib import Path
from rag_search.core.config import INDEX_ROOT, ProjectEntry, index_dir
from rag_search.core.orphans import TRASH_DIRNAME, expire_trash, orphan_dirs, quarantine
from rag_search.core.registry import upsert_project
scenario, base = sys.argv[1], Path(sys.argv[2])
trash = INDEX_ROOT / TRASH_DIRNAME

def store(name, rows=True):
    p = base / name
    p.mkdir(parents=True, exist_ok=True)
    d = index_dir(str(p))
    d.mkdir(parents=True, exist_ok=True)
    (d / "vectors.db").write_bytes(b"x" * 4096)
    if rows:
        upsert_project(ProjectEntry(path=str(p), enabled=True))
    return p, d

def names():
    return sorted(p.name for p in trash.iterdir()) if trash.exists() else []
live, live_dir = store("kept")
gone, gone_dir = store("dropped", rows=False)          # a store with no row: a real orphan
out = {}

if scenario == "sweep":
    from rag_search.daemon.sweeps import maintenance
    maintenance()
    out["orphan_gone_from_root"] = not gone_dir.exists()
    out["live_survived"] = live_dir.exists()
    out["trash_entries"] = names()
    # Guarded: with quarantine broken there is no trash dir at all, and an unguarded iterdir would
    # report that as a child crash rather than as the empty result the assertion can name.
    out["recovered_bytes"] = [(q / "vectors.db").stat().st_size
                              for q in (trash.iterdir() if trash.exists() else [])
                              if (q / "vectors.db").exists()]
    maintenance()          # a sweep that re-collects its own trash is a slower delete
    out["orphans_seen_after"] = [str(d) for d in orphan_dirs()]
    out["trash_after_second_pass"] = names()
elif scenario == "expire":
    q = quarantine(gone_dir)
    os.utime(q, (time.time() - 8 * 86400,) * 2)
    fresh = quarantine(live_dir)
    out["expired"] = [Path(p).name for p in expire_trash()]
    out["old_gone"] = not Path(q).exists()
    out["fresh_kept"] = fresh is not None and Path(fresh).exists()
elif scenario == "collision":
    a = quarantine(gone_dir)
    store("dropped", rows=False)                       # same name, quarantined again same second
    b = quarantine(gone_dir)
    out["both"] = [Path(a).name if a else None, Path(b).name if b else None]
    out["distinct"] = bool(a and b and a != b and Path(a).exists() and Path(b).exists())
elif scenario == "suitepurge":
    from tests.live._sample_workspace import (index_dir_names,
                                              purge_unowned_index_dirs_created_since)
    before = index_dir_names()                         # snapshot taken with no trash present
    q = quarantine(gone_dir)                           # ...which appears mid-run, as it really does
    out["in_before"] = TRASH_DIRNAME in before
    out["purged"] = purge_unowned_index_dirs_created_since(before)
    out["quarantine_survived"] = Path(q).exists()

print(json.dumps(out))
"""


def _run(tmp: Path, scenario: str) -> dict:
    env = {**os.environ,
           "RSE_REGISTRY_PATH": str(tmp / "projects.json"),
           "RSE_INDEX_ROOT": str(tmp / "indexes"),
           "PYTHONPATH": str(Path(__file__).parents[2])}
    (tmp / "ws").mkdir(parents=True, exist_ok=True)
    r = subprocess.run([sys.executable, "-c", _CHILD, scenario, str(tmp / "ws")],
                       capture_output=True, text=True, env=env, timeout=180)
    assert r.returncode == 0, f"child exit {r.returncode}:\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_qt1_the_sweep_moves_the_orphan_aside_instead_of_deleting_it(safe_tmp_path):
    """QT1: after the unattended sweep the orphan is out of INDEX_ROOT and its bytes still exist."""
    got = _run(safe_tmp_path / "qt1", "sweep")

    assert got["orphan_gone_from_root"], f"the sweep left the orphan in place: {got}"
    assert got["live_survived"], f"the sweep took a registered project's store: {got}"
    assert len(got["trash_entries"]) == 1, f"the orphan was not quarantined: {got}"
    assert got["recovered_bytes"] == [4096], (
        f"the quarantined store is empty — moved as a name, not as data: {got}")


def test_qt2_the_sweep_does_not_re_collect_its_own_trash(safe_tmp_path):
    """QT2: `.trash` owns no registry row, which is the entire test the sweep applies. Without the
    skip the second pass quarantines it into itself — and every QT1 assertion still passes, because
    QT1 only ever looks after one pass."""
    got = _run(safe_tmp_path / "qt2", "sweep")

    assert got["orphans_seen_after"] == [], (
        f"the sweep sees its own quarantine as an orphan: {got['orphans_seen_after']}")
    assert got["trash_after_second_pass"] == got["trash_entries"], (
        f"a second sweep pass disturbed the quarantine: {got}")


def test_qt3_quarantine_expires_after_a_week_but_not_before(safe_tmp_path):
    """QT3: the fresh arm carries the weight — an inverted comparison passes the old-one-is-gone
    assertion perfectly, and an expiry that fires early is just a delete with extra steps."""
    got = _run(safe_tmp_path / "qt3", "expire")

    assert got["old_gone"] and len(got["expired"]) == 1, (
        f"an 8-day-old quarantined store was not expired: {got}")
    assert got["fresh_kept"], f"a just-quarantined store was expired immediately: {got}"


def test_qt4_the_live_suites_own_purge_spares_the_quarantine(safe_tmp_path):
    """QT4: the third skip site, and the only one with no confirmation in front of it."""
    got = _run(safe_tmp_path / "qt4", "suitepurge")

    assert not got["in_before"], "the before-picture counts .trash as a store"
    assert got["quarantine_survived"], (
        f"the suite's own teardown deleted the quarantine and the fleet stores in it: {got}")


def test_qt5_a_name_collision_does_not_lose_the_second_store(safe_tmp_path):
    """QT5: `rename` onto an existing dir fails on Linux rather than merging, so without the
    uniquifier the second store is never quarantined and the operator's count is wrong."""
    got = _run(safe_tmp_path / "qt5", "collision")

    assert got["distinct"], f"the second quarantine collided with the first: {got['both']}"
