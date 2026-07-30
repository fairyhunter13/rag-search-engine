"""CO1-CO2: `clean-orphans` deletes only index dirs that no registered project owns.

The guard compared a registry *path* against an index dir *name* (`<basename>-<sha16>`), which can
never contain it — so it matched nothing and called all 179 dirs on this fleet orphans, live stores
included. `--yes` would have deleted the whole fleet's index and cost a full GPU re-index of 140
projects to rebuild. Both directions are asserted: a fix that reports nothing at all would satisfy
"the live store survives" on its own.

Isolated by environment, not by patching: `RSE_INDEX_ROOT` and `RSE_REGISTRY_PATH` are read at
import time, so a subprocess with both pointed into tmp_path runs the real command against a real
registry file and a real index tree while touching neither the fleet's registry nor its stores.
No model is loaded — this is filesystem and registry behaviour only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

ORPHAN = "nobody-0123456789abcdef"


def _layout(tmp: Path) -> tuple[Path, Path, Path]:
    """A registry holding one project, and an index tree holding its store plus one orphan.

    The live dir's name comes from `index_dir` itself rather than a hardcoded hash, so the test
    still points at the right directory if the naming scheme changes.
    """
    from rag_search.core.config import index_dir

    project = tmp / "proj"
    (project / "src").mkdir(parents=True)
    (tmp / "projects.json").write_text(json.dumps({str(project): {"enabled": True}}))

    indexes = tmp / "indexes"
    live = indexes / index_dir(str(project)).name
    orphan = indexes / ORPHAN
    for d in (live, orphan):
        d.mkdir(parents=True)
        (d / "vectors.db").write_bytes(b"SQLite format 3\x00")
    return project, live, orphan


def _run(tmp: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ,
           "RSE_INDEX_ROOT": str(tmp / "indexes"),
           "RSE_REGISTRY_PATH": str(tmp / "projects.json")}
    exe = Path(sys.executable).with_name("rag-search")
    r = subprocess.run([str(exe), "clean-orphans", *args],
                       capture_output=True, text=True, env=env, timeout=180)
    assert r.returncode == 0, f"clean-orphans exit {r.returncode}: {r.stdout}\n{r.stderr}"
    return r


def test_co1_dry_run_names_the_orphan_and_spares_the_live_store(safe_tmp_path):
    """CO1: the report must name the unowned dir and only that one."""
    tmp = safe_tmp_path / "co1"
    tmp.mkdir(parents=True)
    _project, live, _orphan = _layout(tmp)

    out = _run(tmp).stdout
    assert ORPHAN in out, f"the orphan was not reported: {out}"
    assert live.name not in out, (
        f"a registered project's index dir was reported as an orphan: {out}")
    assert live.exists() and (live / "vectors.db").exists(), "dry run deleted something"


def test_co2_yes_deletes_the_orphan_and_keeps_the_registered_store(safe_tmp_path):
    """CO2: the consequence that matters — deleting a live store means re-embedding it."""
    tmp = safe_tmp_path / "co2"
    tmp.mkdir(parents=True)
    _project, live, orphan = _layout(tmp)

    out = _run(tmp, "--yes").stdout
    assert not orphan.exists(), f"the orphan survived --yes: {out}"
    assert (live / "vectors.db").read_bytes() == b"SQLite format 3\x00", (
        f"--yes deleted a registered project's store: {out}")
    assert "Removed 1." in out, f"expected exactly one removal, got: {out}"


_CO3_CHILD = """
import json, sys
from pathlib import Path
from rag_search.core.config import ProjectEntry, index_dir
from rag_search.core.registry import get_project, upsert_project
from tests.live._sample_workspace import purge_index_dirs_under, purge_project

base = Path(sys.argv[1])
kept, self_removed = str(base / "kept"), str(base / "self-removed")
for p in (kept, self_removed):
    Path(p).mkdir(parents=True)
    upsert_project(ProjectEntry(path=p, enabled=True))
    d = index_dir(p)
    d.mkdir(parents=True)
    (d / "vectors.db").write_bytes(b"SQLite format 3\\x00")
dirs = {"kept": str(index_dir(kept)), "self_removed": str(index_dir(self_removed))}
purge_project(kept)                       # the row-driven path
purge_index_dirs_under(base)              # the tree-walk path, row already gone
print(json.dumps({
    "kept_dir": Path(dirs["kept"]).exists(),
    "self_removed_dir": Path(dirs["self_removed"]).exists(),
    "kept_row": get_project(kept) is not None,
}))
"""


_CO4_CHILD = """
import json, sys
from pathlib import Path
from rag_search.core.config import INDEX_ROOT, ProjectEntry, index_dir
from rag_search.core.registry import upsert_project
from tests.live._sample_workspace import (
    index_dir_names, purge_unowned_index_dirs_created_since)

base = Path(sys.argv[1])
INDEX_ROOT.mkdir(parents=True, exist_ok=True)
(INDEX_ROOT / "preexisting-0123456789abcdef").mkdir()   # a fleet store, present before the run
before = index_dir_names()
owned, unowned = str(base / "owned"), str(base / "unowned")
for p in (owned, unowned):
    Path(p).mkdir(parents=True)
    index_dir(p).mkdir(parents=True)
upsert_project(ProjectEntry(path=owned, enabled=True))   # still registered at teardown
purge_unowned_index_dirs_created_since(before)
print(json.dumps({
    "preexisting": (INDEX_ROOT / "preexisting-0123456789abcdef").exists(),
    "owned": index_dir(owned).exists(),
    "unowned": index_dir(unowned).exists(),
}))
"""


def test_co3_test_teardown_leaves_no_index_dir_behind(safe_tmp_path):
    """CO3: the suite deletes the stores it creates, by row *and* by tree walk.

    Every run builds its workspace under a fresh `mkdtemp`, so an index dir the run does not
    delete is unreachable by name for good — 19 dirs / 62 MB accumulated from one day of runs
    before this. `maintenance()`'s orphan sweep stays the backstop, but it needs a daemon restart
    and unpaused sweeps, so it cannot be the owner.

    Subprocess with RSE_INDEX_ROOT/RSE_REGISTRY_PATH redirected: both are read at import time, so
    this exercises the real helpers against a real registry and index tree without going near the
    fleet's. `self-removed` models the common case of a test dropping its own row in a `finally`,
    leaving the dir with nothing pointing at it.
    """
    tmp = safe_tmp_path / "co3"
    (tmp / "ws").mkdir(parents=True)
    env = {**os.environ,
           "RSE_INDEX_ROOT": str(tmp / "indexes"),
           "RSE_REGISTRY_PATH": str(tmp / "projects.json")}
    r = subprocess.run([sys.executable, "-c", _CO3_CHILD, str(tmp / "ws")],
                       capture_output=True, text=True, env=env, timeout=180)
    assert r.returncode == 0, f"child exit {r.returncode}: {r.stdout}\n{r.stderr}"
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got == {"kept_dir": False, "self_removed_dir": False, "kept_row": False}, (
        f"teardown left state behind: {got}"
    )


def test_co4_the_final_backstop_takes_only_new_unowned_dirs(safe_tmp_path):
    """CO4: the listing-diff sweep must spare pre-existing stores and registered ones alike.

    The one path CO3's two cannot reach: a store written after both the registry row and the source
    tree are gone — a graph-lane pass finishing behind its test, measured once as a dir holding
    `graph.db` and no `vectors.db`. By then only the directory listing names it, and a sweep driven
    by a listing is one wrong predicate away from deleting the fleet, which `clean-orphans` has
    already done once on paper.

    All three arms are asserted because each names a different way of being wrong: drop the snapshot
    and it deletes 139 fleet stores, drop the registry check and it deletes a project indexed for the
    first time during the run, and a sweep that does nothing at all satisfies both of those alone.
    """
    tmp = safe_tmp_path / "co4"
    (tmp / "ws").mkdir(parents=True)
    env = {**os.environ,
           "RSE_INDEX_ROOT": str(tmp / "indexes"),
           "RSE_REGISTRY_PATH": str(tmp / "projects.json")}
    r = subprocess.run([sys.executable, "-c", _CO4_CHILD, str(tmp / "ws")],
                       capture_output=True, text=True, env=env, timeout=180)
    assert r.returncode == 0, f"child exit {r.returncode}: {r.stdout}\n{r.stderr}"
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got == {"preexisting": True, "owned": True, "unowned": False}, (
        f"the backstop took the wrong dirs: {got}")
