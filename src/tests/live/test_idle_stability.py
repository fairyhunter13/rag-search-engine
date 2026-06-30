"""Idle-stability guards (FP/IS series) — GPU-free, no mocks."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def test_source_fingerprint_is_memoized():
    """FP1: second call for a quiescent path must use the cache, not re-walk."""
    import os

    from opencode_search.daemon import sweeps

    tmp_dir = os.path.dirname(__file__)
    sig1 = sweeps._source_fingerprint(tmp_dir)
    assert tmp_dir in sweeps._fingerprint_cache
    coarse, cached_sig = sweeps._fingerprint_cache[tmp_dir]
    assert cached_sig == sig1
    # Stale coarse → re-walk, result must match.
    sweeps._fingerprint_cache[tmp_dir] = (coarse + 1.0, "stale")
    assert sweeps._source_fingerprint(tmp_dir) == sig1


def test_bpre_cascade_debounced():
    """FP2: _regen_owning_processes must skip roots within _BPRE_CASCADE_DEBOUNCE_S."""
    import time

    import opencode_search.core.registry as reg_mod
    import opencode_search.kb.bpre as bpre_mod
    from opencode_search.core.config import ProjectEntry
    from opencode_search.daemon import sweeps

    calls: list[str] = []
    orig_map = sweeps._last_owning_process_regen.copy()
    sweeps._last_owning_process_regen["__fp2__"] = time.monotonic()
    orig_list, orig_bpre = reg_mod.list_projects, bpre_mod.reconstruct_processes
    reg_mod.list_projects = lambda: [ProjectEntry(path="__fp2__", enabled=True, federation=["__m__"])]
    bpre_mod.reconstruct_processes = calls.append
    try:
        sweeps._regen_owning_processes("__m__")
        assert not calls, f"debounce must suppress regen; calls={calls}"
    finally:
        reg_mod.list_projects, bpre_mod.reconstruct_processes = orig_list, orig_bpre
        sweeps._last_owning_process_regen.clear()
        sweeps._last_owning_process_regen.update(orig_map)


def test_federation_cascade_debounced():
    """FP2b: _regen_owning_federations must skip roots within _BPRE_CASCADE_DEBOUNCE_S."""
    import time

    import opencode_search.core.registry as reg_mod
    import opencode_search.kb.wiki as wiki_mod
    from opencode_search.core.config import ProjectEntry
    from opencode_search.daemon import sweeps

    calls: list[str] = []
    orig_map = sweeps._last_owning_federation_regen.copy()
    sweeps._last_owning_federation_regen["__fp2b__"] = time.monotonic()
    orig_list, orig_fed = reg_mod.list_projects, wiki_mod.build_federated_index
    reg_mod.list_projects = lambda: [ProjectEntry(path="__fp2b__", enabled=True, federation=["__m__"])]
    wiki_mod.build_federated_index = calls.append
    try:
        sweeps._regen_owning_federations("__m__")
        assert not calls, f"debounce must suppress regen; calls={calls}"
    finally:
        reg_mod.list_projects, wiki_mod.build_federated_index = orig_list, orig_fed
        sweeps._last_owning_federation_regen.clear()
        sweeps._last_owning_federation_regen.update(orig_map)


def test_reconcile_startup_once_before_while_loop():
    """FP3: reconcile_projects() must appear before any while-True resync loop."""
    import inspect

    from opencode_search.daemon import server as srv_mod

    src = inspect.getsource(srv_mod._start_background)
    rp_pos = src.find("reconcile_projects()")
    while_pos = src.find("while True:")
    assert rp_pos != -1, "reconcile_projects() must be in _start_background source"
    if while_pos != -1:
        assert rp_pos < while_pos, "startup-once call must precede any while-True resync"


def test_reconcile_park_event_wired():
    """FP4: _reconcile_park must exist and be referenced in _start_background."""
    import inspect

    from opencode_search.daemon import server as srv_mod

    assert hasattr(srv_mod, "_reconcile_park")
    assert "_reconcile_park" in inspect.getsource(srv_mod._start_background)


def test_scheduler_uses_deadline_sleep():
    """IS1: Scheduler._loop must compute next-deadline wait, not a fixed tick."""
    import inspect

    from opencode_search.daemon.scheduler import Scheduler

    src = inspect.getsource(Scheduler._loop)
    assert "next_deadline" in src, "Scheduler._loop must compute a next_deadline"
    sig = inspect.signature(Scheduler._loop)
    assert "tick" not in sig.parameters, "Scheduler._loop must not accept a 'tick' parameter"


def test_scheduler_start_no_fixed_tick():
    """IS1b: Scheduler.start must not compute a fixed tick constant."""
    import inspect

    from opencode_search.daemon.scheduler import Scheduler

    assert "tick" not in inspect.getsource(Scheduler.start)


def test_no_junk_paths_in_live_registry(live_client, sample_workspace):
    """IS2: no stale ocs-test-dirs or any _worktrees entries may be enabled.

    The current session's sample_workspace paths are excluded — they are
    legitimately registered for the duration of the test session and torn down
    at session end.  Only entries from previous (leaked) sessions are flagged.
    """
    from opencode_search.core.registry import list_projects
    from tests.live._projects import sample_project_paths

    current_session_paths = sample_project_paths(sample_workspace)
    junk = [
        e.path for e in list_projects()
        if e.enabled and (
            "/_worktrees/" in e.path
            or ("/ocs-test-dirs/" in e.path and e.path not in current_session_paths)
        )
    ]
    assert not junk, (
        f"{len(junk)} junk entries still enabled: {junk[:3]!r}. Prune and restart the daemon."
    )
