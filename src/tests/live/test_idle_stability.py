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
    """IS2: no stale ocs-test-dirs entries may be enabled in the live registry.

    The current session's sample_workspace paths are excluded — they are
    legitimately registered for the duration of the test session and torn down
    at session end.  Only entries from previous (leaked) sessions are flagged.
    Worktrees exclusion is config-driven (OPENCODE_FEDERATION_EXCLUDE), not hardcoded.
    """
    from opencode_search.core.registry import list_projects
    from tests.live._projects import sample_project_paths

    current_session_paths = sample_project_paths(sample_workspace)
    junk = [
        e.path for e in list_projects()
        if e.enabled and "/ocs-test-dirs/" in e.path and e.path not in current_session_paths
    ]
    assert not junk, (
        f"{len(junk)} junk entries still enabled: {junk[:3]!r}. Prune and restart the daemon."
    )


def test_drift_gate_skips_enrich_when_sig_unchanged():
    """FP5: on_change must not call _enrich_project when source fingerprint is unchanged."""
    import os
    import tempfile

    from opencode_search.daemon import sweeps

    calls: list[str] = []
    orig_enrich, orig_idx = sweeps._enrich_project, sweeps._index_files
    with tempfile.TemporaryDirectory() as tmp:
        sig = sweeps._source_fingerprint(tmp)
        sweeps._last_enriched_sig[tmp] = sig
        sweeps._last_index_fail.pop(tmp, None)
        sweeps._last_kb_enrich.pop(tmp, None)
        sweeps._enrich_project = calls.append  # type: ignore[assignment]
        sweeps._index_files = lambda *a, **kw: None  # type: ignore[assignment]
        try:
            sweeps.on_change(tmp, [os.path.join(tmp, "app.log")])
            assert not calls, f"drift gate must suppress enrich when sig unchanged; calls={calls}"
        finally:
            sweeps._enrich_project, sweeps._index_files = orig_enrich, orig_idx
            sweeps._last_enriched_sig.pop(tmp, None)
            sweeps._last_kb_enrich.pop(tmp, None)


def test_drift_gate_triggers_enrich_when_sig_changes():
    """FP6: on_change must call _enrich_project when source fingerprint differs."""
    import tempfile

    from opencode_search.daemon import sweeps

    calls: list[str] = []
    orig_enrich, orig_idx = sweeps._enrich_project, sweeps._index_files
    with tempfile.TemporaryDirectory() as tmp:
        sweeps._last_enriched_sig[tmp] = "stale-sig-will-never-match"
        sweeps._last_index_fail.pop(tmp, None)
        sweeps._last_kb_enrich.pop(tmp, None)
        sweeps._enrich_project = lambda p: calls.append(p)  # type: ignore[assignment]
        sweeps._index_files = lambda *a, **kw: None  # type: ignore[assignment]
        try:
            sweeps.on_change(tmp, [tmp + "/main.go"])
            assert calls, f"enrich must fire when sig differs; calls={calls}"
            assert sweeps._last_enriched_sig.get(tmp) != "stale-sig-will-never-match"
        finally:
            sweeps._enrich_project, sweeps._index_files = orig_enrich, orig_idx
            sweeps._last_enriched_sig.pop(tmp, None)
            sweeps._last_kb_enrich.pop(tmp, None)


def test_watcher_prefers_inotify_over_poll():
    """IS3: Watcher.start() selects watchdog/inotify observer when watchdog is importable."""
    from opencode_search.daemon.watcher import Watcher

    w = Watcher(on_change=lambda p, f: None)
    w.start()
    try:
        assert w._observer is not None, (
            "inotify/watchdog observer must be selected when watchdog is importable"
        )
        assert w._thread is None, (
            "poll fallback thread must NOT start when inotify observer is active"
        )
    finally:
        w.stop()


def test_reconcile_active_flag_lifecycle():
    """FP7: reconcile_projects() must set _reconcile_active during the pass and always clear
    it afterward (including on exception), so _enrich_project's bulk-suppression gate (see
    test_bulk_reconcile_suppresses_member_bpre_fanout) is only ever active during a real pass."""
    import opencode_search.core.registry as reg_mod
    from opencode_search.daemon import sweeps

    assert not sweeps._reconcile_active.is_set(), "flag must start clear"
    observed: list[bool] = []
    orig_list = reg_mod.list_projects

    def _spy():
        observed.append(sweeps._reconcile_active.is_set())
        return []
    reg_mod.list_projects = _spy
    try:
        sweeps.reconcile_projects()
        assert observed and all(observed), "flag must be set while reconcile_projects runs"
        assert not sweeps._reconcile_active.is_set(), "flag must be cleared after a normal return"
    finally:
        reg_mod.list_projects = orig_list

    # Exception mid-pass must still clear the flag (finally-guarded).
    def _boom():
        raise RuntimeError("synthetic failure")
    reg_mod.list_projects = _boom
    try:
        with pytest.raises(RuntimeError):
            sweeps.reconcile_projects()
        assert not sweeps._reconcile_active.is_set(), "flag must be cleared even after an exception"
    finally:
        reg_mod.list_projects = orig_list


def test_reconcile_bpre_root_pass_unconditional():
    """FP8: the reconcile root-pass must call reconstruct_processes on every pass — no
    in-memory sig gate suppressing a repeat call (that gate was restart-fragile; the
    persistent stamp inside reconstruct_processes is now the sole reuse guard, D3)."""
    import opencode_search.core.registry as reg_mod
    import opencode_search.kb.bpre as bpre_mod
    from opencode_search.core.config import ProjectEntry
    from opencode_search.daemon import sweeps

    calls: list[str] = []
    orig_list, orig_bpre = reg_mod.list_projects, bpre_mod.reconstruct_processes
    orig_needs_idx, orig_needs_enrich = sweeps._needs_index, sweeps._needs_enrich
    entry = ProjectEntry(path="__fp8_root__", enabled=True, federation=["__fp8_member__"])
    reg_mod.list_projects = lambda: [entry]
    bpre_mod.reconstruct_processes = lambda p: calls.append(p) or 0
    # Isolate the root-pass: keep the per-project loop a no-op for this fake, unregistered path.
    sweeps._needs_index = lambda p: False
    sweeps._needs_enrich = lambda p: False
    try:
        sweeps.reconcile_projects()
        sweeps.reconcile_projects()
        assert calls == ["__fp8_root__", "__fp8_root__"], (
            f"root-pass must call reconstruct_processes on every pass, no in-memory gate; calls={calls}"
        )
    finally:
        reg_mod.list_projects, bpre_mod.reconstruct_processes = orig_list, orig_bpre
        sweeps._needs_index, sweeps._needs_enrich = orig_needs_idx, orig_needs_enrich


def test_bulk_reconcile_suppresses_member_bpre_fanout():
    """FP9: during a bulk reconcile pass, _enrich_project must NOT fan out to
    _regen_owning_processes or its own self-BPRE reconstruct_processes (Part D1) — the
    reconcile root-pass is the sole BPRE trigger then. Outside reconcile (steady-state
    on_change), both must still fire normally."""
    import shutil
    import tempfile

    import opencode_search.daemon.federation as fed_mod
    import opencode_search.embed.embedder as embed_mod
    import opencode_search.index.indexer as indexer_mod
    import opencode_search.index.store as store_mod
    import opencode_search.kb.bpre as bpre_mod
    import opencode_search.kb.wiki as wiki_mod
    from opencode_search.core.config import index_dir
    from opencode_search.daemon import sweeps
    from opencode_search.graph import llm as llm_mod

    owning_calls: list[str] = []
    bpre_calls: list[str] = []

    class _NoopVectorStore:
        def __init__(self, *a, **kw):
            pass
        def close(self):
            pass

    orig = (
        llm_mod.deepseek_key, wiki_mod.build_federated_index, fed_mod.expand_federation,
        sweeps._regen_owning_processes, bpre_mod.reconstruct_processes,
        embed_mod.get_embedder, indexer_mod.index_docs, store_mod.VectorStore,
    )
    llm_mod.deepseek_key = lambda: "unused-key-bypasses-guard"
    wiki_mod.build_federated_index = lambda root_path: 0
    fed_mod.expand_federation = lambda p: [p, "__fp9_other__"]
    sweeps._regen_owning_processes = owning_calls.append
    bpre_mod.reconstruct_processes = lambda p: bpre_calls.append(p) or 0
    embed_mod.get_embedder = lambda: None
    indexer_mod.index_docs = lambda *a, **kw: 0
    store_mod.VectorStore = _NoopVectorStore

    with tempfile.TemporaryDirectory() as tmp:
        try:
            sweeps._reconcile_active.set()
            try:
                sweeps._enrich_project(tmp)
            finally:
                sweeps._reconcile_active.clear()
            assert not owning_calls and not bpre_calls, (
                f"BPRE fan-out must be suppressed during bulk reconcile; "
                f"owning={owning_calls} bpre={bpre_calls}"
            )

            sweeps._enrich_project(tmp)
            assert owning_calls and bpre_calls, (
                f"BPRE fan-out must fire outside reconcile (steady-state); "
                f"owning={owning_calls} bpre={bpre_calls}"
            )
        finally:
            (
                llm_mod.deepseek_key, wiki_mod.build_federated_index, fed_mod.expand_federation,
                sweeps._regen_owning_processes, bpre_mod.reconstruct_processes,
                embed_mod.get_embedder, indexer_mod.index_docs, store_mod.VectorStore,
            ) = orig
            shutil.rmtree(index_dir(tmp), ignore_errors=True)


def test_kb_heavy_lock_serializes_concurrent_passes():
    """FP10: _KB_HEAVY_LOCK must allow at most one CPU-bound KB pass at a time across
    threads (Part D2) — caps daemon CPU at ~one core instead of pinning two concurrently."""
    import threading
    import time

    from opencode_search.daemon import sweeps

    concurrent = 0
    max_concurrent = 0
    counter_lock = threading.Lock()  # guards the counter itself, not the thing under test

    def _hold():
        nonlocal concurrent, max_concurrent
        with sweeps._KB_HEAVY_LOCK:
            with counter_lock:
                concurrent += 1
                max_concurrent = max(max_concurrent, concurrent)
            time.sleep(0.05)
            with counter_lock:
                concurrent -= 1

    threads = [threading.Thread(target=_hold) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert max_concurrent == 1, f"KB_HEAVY_LOCK must serialize passes; max_concurrent={max_concurrent}"
