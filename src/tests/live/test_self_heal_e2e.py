"""Self-healing pipeline — e2e tests (require GPU; there is no LLM in this lane).

T2 — algorithm-version drift triggers reconcile to re-derive the graph
T3 — source-fingerprint drift triggers reconcile to re-extract new symbols

Both run in a child interpreter with `RSE_REGISTRY_PATH`/`RSE_INDEX_ROOT` redirected, because
`reconcile_projects()` walks the registry via `list_projects` — *all* of it. Run in-process that
registry is the fleet's, so each of these two tests paid a full 149-store walk to assert something
about a two-file temp project, and the walk indexes or re-derives whatever it finds stale. Measured
2026-07-31: one of them held a bounded-parse worker at 98 % of a core for over seven minutes with
real fleet stores open. **The cost is not a constant** — it scales with the fleet and spikes to its
maximum after anything that invalidates every store at once, which `EXTRACTOR_REV` does on each bump,
so the suite got slower as a side effect of shipping the extraction ladder. `test_reconcile_midpass`
was moved off the real registry the same day for exactly this reason (it had wedged a run for 51
minutes); these two are what that pass missed.

Both env vars are read at import time — `registry.py` binds `REGISTRY_PATH` by value and derives
`_LOCK_PATH` from it — so only a fresh interpreter can redirect them. Nothing is substituted: the
walk, the embedder, the extractor and the store stay real, and `test_no_mocks_or_fakes.py` bans the
patching fixture that would be the shortcut. Only the population becomes this test's own.

The child computes facts and prints JSON; every assertion stays in the parent, so a failure reports
what was observed rather than an opaque non-zero exit.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.live


_CHILD = r'''
import json, sys
from pathlib import Path
from rag_search.core.config import ProjectEntry, project_graph_db
from rag_search.core.registry import upsert_project
from rag_search.daemon.sweeps import (
    _code_source_fingerprint, _index_project, _pipeline_algo_version, reconcile_projects)
from rag_search.graph.store import GraphStore

scenario, proj = sys.argv[1], sys.argv[2]
p = Path(proj)
p.mkdir(parents=True, exist_ok=True)
out = {}

if scenario == "algo":
    (p / "a.py").write_text("def foo():\n    bar()\n\ndef bar():\n    pass\n")
    (p / "b.py").write_text("def baz():\n    foo()\n")
    upsert_project(ProjectEntry(path=proj, enabled=True))
    _index_project(proj)

    db = project_graph_db(proj)
    gs = GraphStore(db)
    try:
        out["fresh_stamped"] = gs.get_meta("algo_version") == _pipeline_algo_version()
        gs.set_meta("algo_version", "STALE_ALGO_X")
        gs.commit()
    finally:
        gs.close()

    reconcile_projects()

    gs2 = GraphStore(db)
    try:
        out["restamped"] = gs2.get_meta("algo_version") == _pipeline_algo_version()
        out["source_sig_present"] = gs2.get_meta("source_sig") is not None
        out["l1"] = gs2._con.execute(
            "SELECT COUNT(*) FROM communities WHERE level=1").fetchone()[0]
    finally:
        gs2.close()


elif scenario == "drift":
    (p / "seed.py").write_text("def seed_fn():\n    pass\n")
    upsert_project(ProjectEntry(path=proj, enabled=True))
    _index_project(proj)

    db = project_graph_db(proj)
    gs = GraphStore(db)
    try:
        sig_before = gs.get_meta("source_sig")
        out["sig_before_present"] = sig_before is not None
        out["seed_extracted"] = "seed_fn" in [
            r[0] for r in gs._con.execute("SELECT name FROM symbols").fetchall()]
    finally:
        gs.close()

    (p / "new_module.py").write_text("def brand_new_fn():\n    pass\n")
    sig_new = _code_source_fingerprint(proj)
    out["sig_changed"] = sig_new != sig_before

    reconcile_projects()

    gs2 = GraphStore(db)
    try:
        out["sig_rewritten"] = gs2.get_meta("source_sig") == sig_new
        out["new_extracted"] = "brand_new_fn" in [
            r[0] for r in gs2._con.execute("SELECT name FROM symbols").fetchall()]
    finally:
        gs2.close()

print(json.dumps(out))
'''


def _run(tmp, scenario: str, extra_env: dict | None = None) -> dict:
    """Run one scenario against a registry holding only its own project."""
    root = tmp / scenario
    (root / "ws").mkdir(parents=True)
    env = {**os.environ,
           "RSE_INDEX_ROOT": str(root / "indexes"),
           "RSE_REGISTRY_PATH": str(root / "projects.json"),
           **(extra_env or {})}
    r = subprocess.run([sys.executable, "-c", _CHILD, scenario, str(root / "ws" / "proj")],
                       capture_output=True, text=True, env=env, timeout=600)
    assert r.returncode == 0, f"child exit {r.returncode}: {r.stdout}\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_algo_drift_triggers_rederive(safe_tmp_path):
    """T2: poisoning meta[algo_version] causes reconcile to re-derive the graph."""
    got = _run(safe_tmp_path, "algo")

    assert got["fresh_stamped"], f"fresh index must stamp algo_version: {got}"
    assert got["restamped"], f"reconcile must restamp algo_version after re-derive: {got}"
    assert got["source_sig_present"], f"reconcile must stamp source_sig: {got}"
    assert got["l1"] >= 1, f"re-derive must produce at least 1 L1 community: {got}"


def test_source_drift_triggers_rederive(safe_tmp_path):
    """T3: adding a new source file changes the fingerprint; reconcile re-extracts it.

    `_code_scan` memoises on *elapsed time* (`_CODE_SCAN_TTL_S = 300`), so a fingerprint taken
    immediately after a write is served from the pre-write cache entry by construction — this
    test asserted the memo did not exist and had failed on every run since the memo landed,
    invisibly, because it *was* `@pytest.mark.slow` (that mark is gone since S1/S2,
    2026-07-30 — it cost wall clock, never a model). The memo is correct: it bounds how often a
    43 s fleet-wide stat-walk is paid, against a reconcile cadence measured in half-hours, and
    live edits are the watcher's job rather than reconcile's. So the fix is to opt out through
    the knob production already exposes for exactly this, not to weaken the assertion — the
    invariant under test is "a new file reaches the graph", never "no cache stands in front".
    It must cover `reconcile_projects()` too: with a warm pre-write entry reconcile compares
    stored sig against cached sig, finds them equal, and skips the re-extract this asserts.

    The knob now goes in the child's environment rather than through a fixture that edited this
    process's `os.environ`, which is strictly more faithful — the child reads it at import the way
    production does, and no concurrently-collected test can observe the window in which it was set.
    """
    got = _run(safe_tmp_path, "drift", {"RSE_CODE_SCAN_TTL_S": "0"})

    assert got["sig_before_present"], f"fresh index must stamp source_sig: {got}"
    assert got["seed_extracted"], f"seed_fn must be extracted after first index: {got}"
    assert got["sig_changed"], f"fingerprint must change after adding a file: {got}"
    assert got["sig_rewritten"], f"reconcile must rewrite source_sig to current value: {got}"
    assert got["new_extracted"], (
        f"brand_new_fn must appear in symbols after reconcile re-extracted the graph: {got}")
