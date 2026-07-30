"""IR1-IR4: `index(enabled=False)` on a federation root previews before it deletes.

Removing a root fans out: `expand_federation` returns the root plus every registered member, and the
loop drops each row and `rmtree`s each store. The caller named one path and gets N deletions.

The asymmetry is what makes a confirmation worth a round trip rather than merely polite. A membership
row deleted here comes back on the next federation walk — discovery re-registers members it finds
under a registered root — but the embeddings under it do not. So the recoverable half self-heals and
the expensive half is gone, and the cost of undoing it is GPU time proportional to the member count.

Run against the real tool function with a redirected registry and INDEX_ROOT, not against a copy of
its logic: the fan-out lives in `expand_federation`, and a test that re-derived the member list would
be asserting the preview covers the members the test invented.
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
import asyncio, json, sys
from pathlib import Path
from rag_search.core.config import ProjectEntry, index_dir
from rag_search.core.registry import get_project, upsert_project
from rag_search.server.mcp import index

scenario, base = sys.argv[1], Path(sys.argv[2])
root, members = base / "root", [base / "member-a", base / "member-b"]
for d in (root, *members):
    d.mkdir(parents=True, exist_ok=True)
    index_dir(str(d)).mkdir(parents=True, exist_ok=True)
    (index_dir(str(d)) / "vectors.db").write_bytes(b"x" * 2_000_000)   # 1.9 MB, survives round(,1)
upsert_project(ProjectEntry(path=str(root), enabled=True,
                            federation=[str(m) for m in members]))
for m in members:
    upsert_project(ProjectEntry(path=str(m), enabled=True))

solo = base / "solo"                       # no federation list: nothing fans out from it
solo.mkdir(parents=True, exist_ok=True)
index_dir(str(solo)).mkdir(parents=True, exist_ok=True)
upsert_project(ProjectEntry(path=str(solo), enabled=True))

target = solo if scenario == "solo" else root
kwargs = {"confirm_members": True} if scenario == "confirmed" else {}
out = json.loads(asyncio.run(index(str(target), enabled=False, **kwargs)))

print(json.dumps({
    "out": out,
    "root_row": get_project(str(root)) is not None,
    "member_rows": [get_project(str(m)) is not None for m in members],
    "member_stores": [index_dir(str(m)).exists() for m in members],
    "root_store": index_dir(str(root)).exists(),
    "solo_row": get_project(str(solo)) is not None,
    "solo_store": index_dir(str(solo)).exists(),
    "members": [str(m) for m in members],
}))
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


def test_ir1_removing_a_root_previews_and_deletes_nothing(safe_tmp_path):
    """IR1: the unconfirmed call is inert — every row and every store is still there afterwards."""
    got = _run(safe_tmp_path / "ir1", "preview")

    assert got["out"]["status"] == "confirm_required", f"no preview was returned: {got['out']}"
    assert got["root_row"] and all(got["member_rows"]), f"rows were dropped anyway: {got}"
    assert got["root_store"] and all(got["member_stores"]), f"stores were deleted anyway: {got}"


def test_ir2_the_preview_names_the_members_and_the_cost(safe_tmp_path):
    """IR2: a confirmation prompt that does not say what will go is a speed bump, not information.

    The member list has to come from the same `expand_federation` the deletion loop walks, so the
    two cannot disagree; the sizes are what make "this costs a re-embed" concrete rather than a
    warning the caller learns to click through.
    """
    got = _run(safe_tmp_path / "ir2", "preview")
    out = got["out"]

    assert sorted(out["members"]) == sorted(got["members"]), (
        f"the preview does not name the members that would be deleted: {out}")
    assert len(out["stores"]) == 3, f"the preview skipped a store: {out}"
    assert all(s["mb"] > 0 for s in out["stores"]), (
        f"every store here holds ~1.9 MB, so a zero means the size lookup is not working: {out}")


def test_ir3_confirming_removes_the_root_and_every_member(safe_tmp_path):
    """IR3: the confirmation actually confirms — without this arm the preview is just a broken tool.

    A guard that only ever refuses passes IR1 and IR2 together, and it would be indistinguishable
    from `index(enabled=False)` no longer working at all.
    """
    got = _run(safe_tmp_path / "ir3", "confirmed")

    assert got["out"]["status"] == "removed", f"the confirmed call did not remove: {got['out']}"
    assert sorted(got["out"]["members_removed"]) == sorted(got["members"]), (
        f"the members were not all removed: {got['out']}")
    assert not got["root_row"] and not any(got["member_rows"]), f"rows survived: {got}"
    assert not got["root_store"] and not any(got["member_stores"]), f"stores survived: {got}"


def test_ir4_a_project_with_no_members_still_removes_in_one_call(safe_tmp_path):
    """IR4: the common case must not grow a round trip. Nothing fans out, so nothing to confirm."""
    got = _run(safe_tmp_path / "ir4", "solo")

    assert got["out"]["status"] == "removed", (
        f"a single project was made to confirm a fan-out that cannot happen: {got['out']}")
    assert not got["solo_row"] and not got["solo_store"], f"the solo project survived: {got}"
