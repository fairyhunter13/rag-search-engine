"""C-behaviour: reconcile cooperative-cancellation mid-pass (GPU, no mocks, no real paths).

test_reconcile_pause_stops_mid_pass — pause mid-pass; indexed_count < N (faithful stop test)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.live

_CHILD = r'''
import json, sys, threading, time
from pathlib import Path
from rag_search.core.config import ProjectEntry, project_vector_db
from rag_search.core.registry import upsert_project
from rag_search.daemon import sweeps

base = Path(sys.argv[1])
N = 6
members, vdbs = [base / f"mid{i}" for i in range(N)], []
for m in members:
    m.mkdir(parents=True)
    for j in range(4):
        (m / f"mod{j}.py").write_text(
            f"def func_{j}():\n    return {j}\n\ndef helper_{j}(x):\n    return x + {j}\n")
    upsert_project(ProjectEntry(path=str(m), enabled=True))
    vdbs.append(project_vector_db(str(m)))

done = threading.Event()
def _run():
    try:
        sweeps.reconcile_projects()
    finally:
        done.set()
thread = threading.Thread(target=_run, daemon=True)
thread.start()

# The flip below is the thing under test, so it stays a bare assignment: is_paused() returns True
# on _PAUSED with no deadline, which is exactly the state POST /api/sweeps/pause sets.
deadline, paused_at = time.monotonic() + 240, None
while not done.is_set():
    if any(v.exists() for v in vdbs):
        sweeps._PAUSED = True
        paused_at = time.monotonic()
        break
    if time.monotonic() > deadline:
        break          # reported, not hung — see the parent's assertion on "paused"
    time.sleep(0.05)
thread.join(timeout=300)
print(json.dumps({"indexed": sum(1 for v in vdbs if v.exists()), "n": N,
                  "paused": paused_at is not None, "alive": thread.is_alive(),
                  "latency": None if paused_at is None else round(time.monotonic()-paused_at, 2)}))
'''


def test_reconcile_pause_stops_mid_pass(safe_tmp_path):
    """Cooperative-cancellation: pause mid-pass; fewer than N members must be indexed.

    Six synthetic repos, each needing a real embed. A thread runs the real `reconcile_projects()`;
    the instant the first vector DB appears (provably mid-pass) we set `_PAUSED`. After join,
    `indexed < N` proves the loop-top guard stopped the walk instead of running it to completion.

    Subprocess with RSE_REGISTRY_PATH/RSE_INDEX_ROOT redirected, because `reconcile_projects()`
    walks the registry via `list_projects` — *all* of it. Run in-process that is the fleet's, so
    test's cost was a function of unrelated fleet state: on 07-31 it wedged a run for **51 minutes**
    and was killed, not finished. Its own members sort to positions 72-77 of a 216-row walk (all 78
    never-embedded rows tie on an empty `last_change_seen`, so insertion order decides and these are
    appended last), leaving 72 real un-indexed repos to embed before the first `mid*` store could
    appear. Nothing was wrong with the code under test; the fixture was reading the whole machine.

    Both env vars are read at import time, so only a fresh interpreter can redirect them — the same
    isolation the CO-series uses, and the distinction the 07-30 registry wipe was made of. The walk,
    the embedder, the pause and the store stay real; only the population is now this test's own.
    """
    tmp = safe_tmp_path / "midpass"
    (tmp / "ws").mkdir(parents=True)
    env = {**os.environ,
           "RSE_INDEX_ROOT": str(tmp / "indexes"),
           "RSE_REGISTRY_PATH": str(tmp / "projects.json")}
    r = subprocess.run([sys.executable, "-c", _CHILD, str(tmp / "ws")],
                       capture_output=True, text=True, env=env, timeout=600)
    assert r.returncode == 0, f"child exit {r.returncode}: {r.stdout}\n{r.stderr}"
    got = json.loads(r.stdout.strip().splitlines()[-1])

    assert got["paused"], (
        f"no vdb appeared within the child's 240s budget, so the pause never went in: {got}. "
        "Either the embed is far slower than the ~1.4s/repo the journal records, or reconcile "
        "never reached these rows — check the child registry holds only the 6 members."
    )
    assert got["indexed"] < got["n"], (
        f"pause mid-pass must stop before all {got['n']} members are indexed; got "
        f"indexed={got['indexed']}/{got['n']} — loop-top _PAUSED guard missing from "
        f"reconcile_projects()"
    )
    assert got["indexed"] >= 1, (
        f"pause fired before any member was indexed, so the stop is not mid-pass: {got}"
    )
    print(f"\n[PAUSE-MID] indexed={got['indexed']}/{got['n']}, stop-latency={got['latency']}s")
