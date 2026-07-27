"""Live gates: on_change's heavy half must leave the caller free (no mocks).

Getting the work off the inotify reader was necessary and not sufficient. `_KB_HEAVY_LOCK`
serialises the heavy half regardless, so running it on the watcher's dispatch workers only
ever meant a worker *blocked* on that lock. Measured on the live daemon after the reader fix:
`rse-sweeps-0` inside `bounded_parse` holding the lock, `rse-sweeps-1` parked on it for 56
consecutive py-spy samples (~4 minutes), and a third project's edit never indexed at all
because both workers were consumed. Raising the worker count only moves the number.

So the heavy half now runs on one dedicated lane thread and `on_change` returns after the
cheap half — `_index_files`, which is what actually makes an edit searchable.

Output does not discriminate: the same rows land either way, eventually. The difference is
whether the *caller* is still available, so KL1 is a timing assertion, self-calibrated against
an unblocked run rather than a hardcoded threshold.

KL1  on_change must not block when _KB_HEAVY_LOCK is held by someone else (load-bearing)
KL2  the deferred pass must still happen — returning fast by dropping work would pass KL1
KL3  submits for a busy project coalesce into one queued pass, not a growing backlog
KL4  the lane waits without a timeout — notification-driven, never a poll clock

KL1 and KL2 were demonstrated red against an inline heavy half: KL1 measured on_change at 19.0s
under a 20s lock hold versus 0.1s free, KL2 found the symbol already in graph.db before the join.
KL3 and KL4 are design guards against the realistic next mistakes (a queue.Queue backlog, a
wait() with a timeout); neither can go red against today's code, and that is recorded, not counted
as proof.
"""
from __future__ import annotations

import threading
import time

import pytest

pytestmark = pytest.mark.live


def _seed(safe_tmp_path) -> tuple[str, object]:
    """A real indexed project, built exactly the way the WG gates build theirs."""
    from rag_search.daemon.sweeps import _index_project

    proj = str(safe_tmp_path)
    src = safe_tmp_path / "mod.py"
    src.write_text("def kl_seed():\n    return 1\n")
    _index_project(proj)
    return proj, src


def _fire(proj: str, files: list) -> None:
    """Drive the real on_change with the debounce and reuse stamp cleared (as _fire in WG)."""
    from rag_search.daemon import sweeps
    sweeps._last_kb_enrich.pop(proj, None)
    sweeps._last_enriched_sig.pop(proj, None)
    sweeps._last_index_fail.pop(proj, None)
    sweeps.on_change(proj, [str(f) for f in files])


def _held(seconds: float) -> threading.Event:
    """Hold _KB_HEAVY_LOCK for `seconds` on another thread; returns an 'is held now' event."""
    from rag_search.daemon.sweeps import _KB_HEAVY_LOCK

    holding = threading.Event()

    def hold() -> None:
        with _KB_HEAVY_LOCK:
            holding.set()
            time.sleep(seconds)

    threading.Thread(target=hold, daemon=True).start()
    assert holding.wait(timeout=10.0), "fixture: could not acquire _KB_HEAVY_LOCK"
    return holding


def test_kl1_on_change_does_not_block_on_the_heavy_lock(safe_tmp_path):
    """KL1: a caller must get on_change back while another project's heavy pass is running.

    Self-calibrating: an absolute threshold would either be flaky on a busy box or so loose it
    stopped discriminating. We time an unblocked on_change first, then assert a blocked one is
    not materially slower. Before the lane, the second call waited out the whole lock hold.
    """
    from rag_search.daemon import sweeps

    proj, src = _seed(safe_tmp_path)
    time.sleep(1.1)  # the code sig truncates mtimes to whole seconds
    src.write_text("def kl_seed():\n    return 1\n\n\ndef kl_free():\n    return 2\n")
    t0 = time.monotonic()
    _fire(proj, [src])
    free = time.monotonic() - t0
    assert sweeps._kb_lane_join(timeout=180.0), "KL1: the unblocked pass never finished"

    hold_s = free + 20.0
    _held(hold_s)
    time.sleep(1.1)
    src.write_text("def kl_seed():\n    return 1\n\n\ndef kl_blocked():\n    return 3\n")
    t0 = time.monotonic()
    _fire(proj, [src])
    blocked = time.monotonic() - t0
    assert blocked < free + 5.0, (
        f"KL1: on_change took {blocked:.1f}s with _KB_HEAVY_LOCK held vs {free:.1f}s free "
        f"(lock was held {hold_s:.0f}s) — the caller is still waiting out another project's "
        "pass, which is the starvation the lane exists to end"
    )
    # Drain before yielding: the hold thread still owns _KB_HEAVY_LOCK, and a following test
    # that needs it would fail in its fixture on this test's leftovers rather than its own code.
    assert sweeps._kb_lane_join(timeout=180.0), "KL1: the deferred pass never drained"


def test_kl2_the_deferred_pass_still_happens(safe_tmp_path):
    """KL2: returning fast is only a fix if the work lands anyway.

    Without this, KL1 is satisfied by an on_change that quietly drops the KB half — the exact
    silent-staleness trade WG7 exists to prevent. The symbol must reach graph.db, and it must
    arrive from the lane, after the lock that delayed it is released.
    """
    from rag_search.core.config import project_graph_db
    from rag_search.daemon import sweeps
    from rag_search.graph.store import GraphStore

    def syms() -> set[str]:
        gs = GraphStore(project_graph_db(proj))
        try:
            return {r[0] for r in gs._con.execute("SELECT name FROM symbols")}
        finally:
            gs.close()

    proj, src = _seed(safe_tmp_path)
    assert "kl_seed" in syms(), "KL2: baseline index must contain the seed symbol"

    _held(6.0)
    time.sleep(1.1)
    src.write_text("def kl_seed():\n    return 1\n\n\ndef kl_zzq_deferred():\n    return 4\n")
    _fire(proj, [src])
    assert "kl_zzq_deferred" not in syms(), (
        "KL2: extraction ran inline — on_change was supposed to have handed it to the lane"
    )
    assert sweeps._kb_lane_join(timeout=180.0), "KL2: the lane never finished the deferred pass"
    assert "kl_zzq_deferred" in syms(), (
        "KL2: the deferred pass never landed — on_change returned fast by losing the work"
    )


def test_kl3_submits_for_a_busy_project_coalesce(safe_tmp_path):
    """KL3: a save storm against a project already mid-pass must not grow a queue.

    A `queue.Queue` would satisfy KL1 and KL2 and fail here: every submit would become its own
    full KB pass, so a burst of N saves would cost N federation walks instead of one.
    """
    from rag_search.daemon import sweeps

    proj, src = _seed(safe_tmp_path)
    _held(10.0)
    for i, name in enumerate(("kl_burst_a", "kl_burst_b", "kl_burst_c")):
        time.sleep(1.1)
        src.write_text(f"def kl_seed():\n    return 1\n\n\ndef {name}():\n    return {i}\n")
        _fire(proj, [src])
        if i == 0:
            # Let the lane take the first submit and park on the held lock, so the rest queue
            # behind a genuinely busy lane rather than an idle one.
            time.sleep(1.0)
    with sweeps._kb_lane_cv:
        queued = dict(sweeps._kb_lane_wanted)
    assert list(queued) == [proj], (
        f"KL3: expected exactly one coalesced entry for this project, got {list(queued)}"
    )
    assert sweeps._kb_lane_join(timeout=180.0), "KL3: the lane never drained"


def test_kl4_the_lane_waits_without_a_timeout():
    """KL4: the lane must wake on a submit, never on a clock.

    Every behavioural gate above is equally satisfied by a lane that polls, which would burn a
    wakeup forever on a box whose whole point is a ~1-core budget. Source guard, in the spirit
    of test_no_fixed_interval_timers — and it asserts the lane exists, or it passes vacuously.
    """
    import ast
    import inspect

    from rag_search.daemon import sweeps

    tree = ast.parse(inspect.getsource(sweeps._kb_lane_run))
    waits = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "wait"]
    assert waits, "KL4: no wait() in the lane loop — it is not blocking on notifications at all"
    for w in waits:
        assert not w.args and not w.keywords, (
            "KL4: the lane's wait() carries a timeout — that is a poll loop wearing a "
            "Condition's coat, and the only clock in this daemon is the kernel's"
        )
