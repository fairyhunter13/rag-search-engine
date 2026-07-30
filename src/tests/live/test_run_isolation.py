"""CL1-CL3 — overlapping live runs must not destroy each other's state (W2's C1).

The session fixture `_purge_leaked_test_state` self-heals state a *killed* prior run leaked. Until
2026-07-30 it did that by deleting every child of `rse-test-dirs` **on existence**, and its own
docstring asserted the premise that made that safe: "at session start the current run hasn't built
its own workspace yet, so anything here belongs to a dead prior session." That premise is false the
moment two runs overlap, and overlapping runs are the normal state in this checkout — three agent
profiles share one tree, and the pytest-vs-pytest gate in `conftest.py` catches a concurrent
*start*, not a run that begins while another is already mid-suite. The damage never announced
itself: a live run lost its workspace and failed somewhere downstream with an unrelated assertion.

**These tests drive the collision rather than assert a state.** That distinction is FE12's lesson,
and C1 is the same shape one layer down — a state assertion cannot tell "nothing can break this"
from "nothing has broken it lately", and here the broken thing was written down in a docstring and
never executed. So CL1 spawns a *real* second pytest process, tags a directory with its pid, and
runs the real purge against it. Delete the `owner_is_live` skip and CL1/CL2 go red.

Scoped to a sandbox *inside* the test base, never `_SAFE_BASE` itself: `purge_dead_owned_dirs` is
destructive, and `assert_under_test_base`'s docstring records what a wrong predicate cost once.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.live._projects import _boot_id, make_run_dir, owner_is_live, owner_tag
from tests.live._sample_workspace import purge_dead_owned_dirs, purge_rows_under

pytestmark = pytest.mark.live

# A pid that is live *and* pytest is the only thing that licenses skipping a purge, so the negative
# case needs a pid that is neither. Kernel pid_max is at least 32768 on every Linux; 2**22 is above
# even the 4194304 ceiling a tuned host can set, so no process can ever hold it.
_IMPOSSIBLE_PID = 2 ** 22


def _holder(tmp_path: Path) -> subprocess.Popen:
    """A real pytest process that stays alive for the duration of one test."""
    script = tmp_path / "test_hold.py"
    script.write_text("import time\n\n\ndef test_hold():\n    time.sleep(30)\n")
    # cwd is the tmp dir, and that is load-bearing twice over: `_contending_live_runs` only counts
    # a pytest whose cwd is this checkout, so the holder cannot trip the one-suite-at-a-time gate
    # for a concurrent session; and pytest's rootdir search from there finds no pyproject.toml, so
    # it runs without this repo's addopts, markers or plugins.
    p = subprocess.Popen(
        [sys.executable, "-m", "pytest", str(script), "-q", "-p", "no:cacheprovider"],
        cwd=str(tmp_path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            if b"pytest" in Path(f"/proc/{p.pid}/cmdline").read_bytes():
                return p
        except OSError:
            pass
        time.sleep(0.05)
    p.kill()
    pytest.fail("holder pytest process never became visible in /proc within 15 s")


def test_cl1_a_live_run_keeps_its_directory_while_a_dead_one_is_purged(tmp_path: Path) -> None:
    """CL1 — the purge spares a directory a concurrently-running pytest owns.

    Three children, one purge call, and the assertions are about *which* survived: the collision
    itself, not a property of the predicate in isolation. The dead-owner and untagged children are
    in the same call because sparing everything would pass a test that only checked the live one —
    and that would silently retire the self-heal this fixture exists for.
    """
    sandbox = make_run_dir("c1-")
    holder = _holder(tmp_path)
    try:
        live = sandbox / f"rseown-{_boot_id()}-{holder.pid}-workspace"
        dead = sandbox / f"rseown-{_boot_id()}-{_IMPOSSIBLE_PID}-leaked"
        plain = sandbox / "untagged-leftover"
        for d in (live, dead, plain):
            d.mkdir()
            (d / "marker").write_text("x")

        purge_dead_owned_dirs(sandbox)

        assert live.is_dir(), (
            "the purge deleted a directory owned by a live pytest process — this is the C1 "
            "collision, and it is how a concurrent run loses its workspace mid-suite")
        assert (live / "marker").is_file(), "live owner's directory survived but was emptied"
        assert not dead.exists(), (
            "a dead owner's directory survived — the self-heal for state a killed run leaks is "
            "what this fixture was built for and must keep working")
        assert not plain.exists(), (
            "an untagged directory survived; untagged means pre-C1 or foreign, and purging those "
            "is the behaviour the ownership tag narrows, not the behaviour it removes")
    finally:
        holder.kill()
        holder.wait(timeout=30)


def test_cl2_a_live_runs_registry_rows_survive_the_purge(tmp_path: Path) -> None:
    """CL2 — the registry half of the same collision.

    `purge_rows_under` drops every row pointing into the test base, and a dropped row deregisters a
    project the other suite is actively asserting against — the directory can survive and the run
    still fails, because a test that resolves its project by registry lookup no longer finds it.
    Both halves key on the same `owner_is_live`, so this is a second call site of one rule rather
    than a second rule; testing only the directory half would leave the other free to drift.

    **They do not key on the same *input*, and the first draft of this test got that wrong.** The
    directory purge asks about a child's `name`; the registry purge asks about a whole `path`, and
    `_OWNER_RE.search` finds a tag anywhere in it — so every file under a live-tagged workspace is
    spared by its ancestor's tag, which is exactly what keeps `…/sample-ws-<tag>/shop-federation/
    promo-svc` alive. Both are right for what they are given, but it means the sandbox here cannot
    itself be tagged: under `make_run_dir` the dead-owner row inherited *this* run's live tag and
    survived, and the failure read as the fix not working. An untagged sandbox is the price, and it
    is a real one — a concurrent run's session-start purge would take it — so it is created and
    torn down inside one test rather than held.
    """
    import tempfile

    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import list_projects, remove_project, upsert_project
    from tests.live._projects import _SAFE_BASE

    sandbox = Path(tempfile.mkdtemp(dir=_SAFE_BASE, prefix="c1-rows-"))
    holder = _holder(tmp_path)
    live = sandbox / f"rseown-{_boot_id()}-{holder.pid}-proj"
    dead = sandbox / f"rseown-{_boot_id()}-{_IMPOSSIBLE_PID}-proj"
    try:
        for d in (live, dead):
            d.mkdir()
            upsert_project(ProjectEntry(path=str(d), enabled=True))

        purge_rows_under(sandbox)

        paths = {e.path for e in list_projects()}
        assert str(live) in paths, (
            "a live run's registry row was dropped — its tests can no longer resolve the project "
            "they were built against, and the failure surfaces nowhere near this cause")
        assert str(dead) not in paths, "a dead run's leaked row survived; IS2 fails on that junk"
    finally:
        holder.kill()
        holder.wait(timeout=30)
        remove_project(str(live))
        shutil.rmtree(sandbox, ignore_errors=True)


def test_cl3_ownership_is_keyed_on_the_boot_and_the_process_kind() -> None:
    """CL3 — the tag's two qualifiers, each of which alone would be wrong.

    Pid alone is not identity: pids recycle, and across a reboot a leaked directory's pid is very
    likely live again as something unrelated — which would make the purge spare leaked state
    forever, turning C1's fix into a slow leak instead of a fast collision. And a live pid that is
    *not* pytest cannot own suite state, so it must not veto the purge either. Asserted on this
    process, whose boot id is current and whose cmdline really is pytest, and on a synthetic tag
    from a boot that is not this one.
    """
    assert owner_is_live(f"sample-ws-{owner_tag()}-abc123"), \
        "this pytest process does not recognise its own tag"
    assert not owner_is_live(f"sample-ws-rseown-deadbeef-{os.getpid()}-abc123"), \
        "a tag from another boot was treated as live; pids recycle across reboots"
    assert not owner_is_live(f"sample-ws-rseown-{_boot_id()}-{_IMPOSSIBLE_PID}-abc"), \
        "an unallocatable pid was treated as live"
    assert not owner_is_live("plain-tmpdir-name"), "an untagged name must purge as before"
