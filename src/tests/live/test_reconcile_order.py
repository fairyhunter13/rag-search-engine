"""RO1-RO3: the reconcile walk reaches the projects that have nothing before the ones that are stale.

`reconcile_projects` returns on `is_paused()` and keeps no resume cursor, so every pass restarts at
position 0 and only ever completes the prefix. That makes the walk *order* the thing that decides
which projects are reachable at all, not merely the order they are reached in — and the order was
keyed on `last_change_seen`, which a never-indexed project does not have.

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
import json, sys
from pathlib import Path
from rag_search.core.config import INDEX_ROOT, ProjectEntry, index_dir
from rag_search.daemon.sweeps import reconcile_order

def row(name, seen, vectors):
    p = f"/nonexistent/{name}"
    if vectors:
        d = index_dir(p)
        d.mkdir(parents=True, exist_ok=True)
        (d / "vectors.db").write_bytes(b"x")
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
    """One child for all three arms — they read one ordering, they do not each need their own.

    Module-scoped rather than using function-scoped `safe_tmp_path` because nothing here mutates
    what it reads. The dir still lives under the suite's own base, which is what
    `assert_under_test_base` and `test_no_real_project_in_tests.py` are written against.
    """
    import shutil
    import tempfile
    base = Path.home() / ".local" / "share" / "rse-test-dirs"
    base.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(dir=base, prefix="ro-"))
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
