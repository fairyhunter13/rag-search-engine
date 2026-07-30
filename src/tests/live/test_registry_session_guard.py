"""RG-S1..RG-S5: the session tripwire that would have caught the 2026-07-30 fleet wipe.

`_registry_guard` is the backstop for every in-process caller of a mutating registry API, so it is
tested the way the thing it guards must be run: **in a subprocess with `RSE_REGISTRY_PATH`
redirected**, never in-process. That distinction is not stylistic — running the wipe in-process
against the real `projects.json` is literally the incident this module exists for, and a test that
demonstrated it that way would reproduce it.

Both directions are asserted, because only one of them is load-bearing. A guard that flags every
absent row would pass "it catches the wipe" and still be useless: `_migrate()` legitimately prunes
dead registrations and re-keys canonical paths on ordinary runs, so a guard that cannot tell those
from a deletion is a timer, not a monitor, and gets muted in a week. RG-S3/S4/S5 are that half.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

# The child mirrors the fixture's own sequence — snapshot, mutate, detect, restore — against a
# redirected registry, so it exercises the real flock and the real `_load`/`upsert_project` path.
_CHILD = r"""
import json, sys
from pathlib import Path
from rag_search.core.config import ProjectEntry
from rag_search.core.registry import remove_project, upsert_project
from tests.live._registry_guard import restore_rows, rows_lost_outside, take_snapshot

scenario, base = sys.argv[1], Path(sys.argv[2])

real = base / "real-project"       # outside the suite base, exists on disk -> protected
suite = base / "rse-test-dirs" / "ws"   # inside the suite base -> purged by design
gone = base / "deleted-project"    # registered, then removed from disk -> _migrate prunes it
target = base / "canonical-project"     # the re-key arm needs a genuine symlink: canonicalize_path
link = base / "linked-project"          # is Path.resolve(), so it is identity on a plain path and
target.mkdir(parents=True, exist_ok=True)   # a same-path "re-key" would prove nothing at all.
if not link.exists():
    link.symlink_to(target, target_is_directory=True)
for d in (real, suite, gone, link):
    d.mkdir(parents=True, exist_ok=True)
    upsert_project(ProjectEntry(path=str(d)))

snapshot, rows_at_start = take_snapshot()

if scenario == "wipe":              # the incident: every row removed in-process
    for p in list(rows_at_start):
        remove_project(p)
elif scenario == "suite_only":      # what a healthy run does to its own rows
    remove_project(str(suite))
elif scenario == "vanished":        # tree deleted on disk, row dropped as self-heal
    import shutil; shutil.rmtree(gone); remove_project(str(gone))
elif scenario == "rekeyed":         # the symlink key gives way to the real path it resolves to
    remove_project(str(link)); upsert_project(ProjectEntry(path=str(target)))
elif scenario == "clean":           # nothing removed at all
    pass

lost = rows_lost_outside(rows_at_start, base / "rse-test-dirs")
failed = restore_rows(rows_at_start, lost)
print(json.dumps({
    "lost": lost,
    "failed": failed,
    "snapshot_exists": snapshot.exists(),
    "rows_after_restore": sorted(json.loads(Path(sys.argv[3]).read_text())),
    "real": str(real), "suite": str(suite), "gone": str(gone), "link": str(link),
}))
"""


def _run(tmp: Path, scenario: str) -> dict:
    registry = tmp / "projects.json"
    env = {**os.environ,
           "RSE_REGISTRY_PATH": str(registry),
           "RSE_INDEX_ROOT": str(tmp / "indexes"),
           "PYTHONPATH": str(Path(__file__).parents[2])}
    r = subprocess.run([sys.executable, "-c", _CHILD, scenario, str(tmp), str(registry)],
                       capture_output=True, text=True, env=env, timeout=180)
    assert r.returncode == 0, f"child exit {r.returncode}:\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_rgs1_a_full_wipe_is_named_and_restored(safe_tmp_path):
    """RG-S1: the incident's exact shape — every row removed in-process — is caught and undone."""
    out = _run(safe_tmp_path / "rgs1", "wipe")

    assert out["real"] in out["lost"], (
        f"a real row outside the suite base was deleted and the guard did not name it: {out}")
    assert not out["failed"], f"restore failed for {out['failed']}"
    assert out["real"] in out["rows_after_restore"], (
        f"the guard named the row but did not put it back: {out}")


def test_rgs2_the_snapshot_survives_on_disk(safe_tmp_path):
    """RG-S2: the operator's handle outlives the run — restoration is not the only recovery."""
    out = _run(safe_tmp_path / "rgs2", "wipe")
    assert out["snapshot_exists"], f"no snapshot file was left behind: {out}"


def test_rgs3_the_suites_own_rows_are_not_a_violation(safe_tmp_path):
    """RG-S3: `purge_rows_under(_SAFE_BASE)` runs on every healthy session and must stay silent."""
    out = _run(safe_tmp_path / "rgs3", "suite_only")
    assert out["lost"] == [], (
        f"the guard flagged the suite purging its own rows — it would fire on every green run: {out}")


def test_rgs4_a_row_whose_tree_vanished_is_not_a_violation(safe_tmp_path):
    """RG-S4: `_migrate()` prunes dead registrations. That is the self-heal working, not a wipe."""
    out = _run(safe_tmp_path / "rgs4", "vanished")
    assert out["lost"] == [], f"the guard flagged a legitimate dead-registration prune: {out}"


def test_rgs5_a_rekeyed_row_is_not_a_violation(safe_tmp_path):
    """RG-S5: `_migrate()` re-keys a symlink registration to the real path it resolves to."""
    out = _run(safe_tmp_path / "rgs5", "rekeyed")
    assert out["lost"] == [], f"the guard flagged a canonical re-key as a deletion: {out}"


def test_rgs6_a_clean_run_reports_nothing(safe_tmp_path):
    """RG-S6: the null case — a run that removes nothing must be silent.

    RG-S3..S6 are all satisfied by a guard hardcoded to `return []`; RG-S1 is the arm that rules
    that out. Neither half means anything without the other, which is the point of writing both.
    """
    out = _run(safe_tmp_path / "rgs6", "clean")
    assert out["lost"] == [], f"the guard fired on a run that removed nothing: {out}"
    assert out["real"] in out["rows_after_restore"], f"a clean run lost a row: {out}"
