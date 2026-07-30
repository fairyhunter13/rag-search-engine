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
