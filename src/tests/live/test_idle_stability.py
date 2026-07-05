"""Idle-stability guards (FP/IS series) — GPU-free, no mocks."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def test_source_fingerprint_is_memoized():
    """FP1: second call for a quiescent path must use the cache, not re-walk."""
    import os

    from rag_search.daemon import sweeps

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

    import rag_search.core.registry as reg_mod
    import rag_search.kb.bpre as bpre_mod
    from rag_search.core.config import ProjectEntry
    from rag_search.daemon import sweeps

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

    import rag_search.core.registry as reg_mod
    import rag_search.kb.wiki as wiki_mod
    from rag_search.core.config import ProjectEntry
    from rag_search.daemon import sweeps

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

    from rag_search.daemon import server as srv_mod

    src = inspect.getsource(srv_mod._start_background)
    rp_pos = src.find("reconcile_projects()")
    while_pos = src.find("while True:")
    assert rp_pos != -1, "reconcile_projects() must be in _start_background source"
    if while_pos != -1:
        assert rp_pos < while_pos, "startup-once call must precede any while-True resync"


def test_reconcile_park_event_wired():
    """FP4: _reconcile_park must exist and be referenced in _start_background."""
    import inspect

    from rag_search.daemon import server as srv_mod

    assert hasattr(srv_mod, "_reconcile_park")
    assert "_reconcile_park" in inspect.getsource(srv_mod._start_background)


def test_scheduler_uses_deadline_sleep():
    """IS1: Scheduler._loop must compute next-deadline wait, not a fixed tick."""
    import inspect

    from rag_search.daemon.scheduler import Scheduler

    src = inspect.getsource(Scheduler._loop)
    assert "next_deadline" in src, "Scheduler._loop must compute a next_deadline"
    sig = inspect.signature(Scheduler._loop)
    assert "tick" not in sig.parameters, "Scheduler._loop must not accept a 'tick' parameter"


def test_scheduler_start_no_fixed_tick():
    """IS1b: Scheduler.start must not compute a fixed tick constant."""
    import inspect

    from rag_search.daemon.scheduler import Scheduler

    assert "tick" not in inspect.getsource(Scheduler.start)


def test_no_junk_paths_in_live_registry(live_client, sample_workspace):
    """IS2: no stale ocs-test-dirs entries may be enabled in the live registry.

    The current session's sample_workspace paths are excluded — they are
    legitimately registered for the duration of the test session and torn down
    at session end.  Only entries from previous (leaked) sessions are flagged.
    Worktrees exclusion is config-driven (OPENCODE_FEDERATION_EXCLUDE), not hardcoded.
    """
    from rag_search.core.registry import list_projects
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

    from rag_search.daemon import sweeps

    calls: list[str] = []
    orig_enrich, orig_idx = sweeps._enrich_project, sweeps._index_files
    with tempfile.TemporaryDirectory() as tmp:
        sig = sweeps._code_source_fingerprint(tmp)
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

    from rag_search.daemon import sweeps

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


# ─── HR38: on_change's code-only cascade gate (mirrors BPS1-4 for _bpre_code_sig) ──────────
# The 4th idle-CPU root cause the code-only fingerprint closes: on_change's own drift gate
# was keyed on the all-files _source_fingerprint, so docs/wiki/config/image churn kept waking
# the cascade even though HR36 already made BPRE's own reuse stamp code-only.


@pytest.fixture
def _fcg_project():
    """A real tmp project, one code file, _enrich_project/_index_files stubbed to a call list."""
    import tempfile

    from rag_search.daemon import sweeps

    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, {"main.py": "def f():\n    pass\n"})
        calls: list[str] = []
        orig_enrich, orig_idx = sweeps._enrich_project, sweeps._index_files
        sweeps._enrich_project = calls.append  # type: ignore[assignment]
        sweeps._index_files = lambda *a, **kw: None  # type: ignore[assignment]
        sweeps._last_index_fail.pop(tmp, None)
        sweeps._last_kb_enrich.pop(tmp, None)
        sweeps._last_enriched_sig.pop(tmp, None)
        try:
            sweeps.on_change(tmp, [tmp + "/main.py"])  # baseline stamp
            assert calls == [tmp], "baseline on_change must enrich exactly once"
            yield tmp, calls, sweeps
        finally:
            sweeps._enrich_project, sweeps._index_files = orig_enrich, orig_idx
            sweeps._last_enriched_sig.pop(tmp, None)
            sweeps._last_kb_enrich.pop(tmp, None)
            sweeps._last_index_fail.pop(tmp, None)


def test_fcg1_docs_wiki_churn_quiescent(_fcg_project):
    """FCG1: editing docs/*.md + generated wiki/*.md after the baseline must not re-enrich."""
    tmp, calls, sweeps = _fcg_project
    sweeps._last_kb_enrich.pop(tmp, None)  # bypass the 45s debounce for this scenario
    _write_tree(tmp, {"docs/notes.md": "hello\n", "wiki/L1_overview.md": "generated\n"})
    sweeps.on_change(tmp, [tmp + "/docs/notes.md", tmp + "/wiki/L1_overview.md"])
    assert calls == [tmp], f"docs/wiki-only churn must not re-trigger the cascade; calls={calls}"


def test_fcg2_config_image_churn_quiescent(_fcg_project):
    """FCG2: editing config (*.json) + image (*.png) after the baseline must not re-enrich."""
    tmp, calls, sweeps = _fcg_project
    sweeps._last_kb_enrich.pop(tmp, None)
    _write_tree(tmp, {"config/settings.json": '{"a": 1}\n', "assets/logo.png": "not-a-png\n"})
    sweeps.on_change(tmp, [tmp + "/config/settings.json", tmp + "/assets/logo.png"])
    assert calls == [tmp], f"config/image-only churn must not re-trigger the cascade; calls={calls}"


def test_fcg3_real_code_drift_fires_cascade_once(_fcg_project):
    """FCG3: editing main.py after the baseline must re-enrich exactly once — gate not inert."""
    import time
    tmp, calls, sweeps = _fcg_project
    sweeps._last_kb_enrich.pop(tmp, None)
    time.sleep(1.1)  # ensure a distinct mtime tick (sig truncates to whole seconds)
    _write_tree(tmp, {"main.py": "def f():\n    pass\n\ndef g():\n    pass\n"})
    sweeps.on_change(tmp, [tmp + "/main.py"])
    assert calls == [tmp, tmp], f"real code drift must re-trigger the cascade once; calls={calls}"


def test_fcg4_convergence_second_call_reuses(_fcg_project):
    """FCG4: a second consecutive on_change with no change since baseline must not re-enrich."""
    tmp, calls, sweeps = _fcg_project
    sweeps._last_kb_enrich.pop(tmp, None)  # bypass debounce so only the sig gate is exercised
    sweeps.on_change(tmp, [tmp + "/main.py"])
    assert calls == [tmp], f"unchanged second call must reuse, not re-enrich; calls={calls}"


def test_watcher_prefers_inotify_over_poll():
    """IS3: Watcher.start() runs one watchfiles (Rust notify) thread — no hand-rolled poll loop."""
    from rag_search.daemon.watcher import Watcher

    w = Watcher(on_change=lambda p, f: None)
    w.start()
    try:
        assert w._thread is not None and w._thread.is_alive(), (
            "watcher thread must be running"
        )
        assert w._thread.name == "ocs-watcher", "single unified watchfiles thread expected"
    finally:
        w.stop()
    assert not w._thread.is_alive(), "watcher thread must stop cleanly"


def test_reconcile_active_flag_lifecycle():
    """FP7: reconcile_projects() must set _reconcile_active during the pass and always clear
    it afterward (including on exception), so _enrich_project's bulk-suppression gate (see
    test_bulk_reconcile_suppresses_member_bpre_fanout) is only ever active during a real pass."""
    import rag_search.core.registry as reg_mod
    from rag_search.daemon import sweeps

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
    import rag_search.core.registry as reg_mod
    import rag_search.kb.bpre as bpre_mod
    from rag_search.core.config import ProjectEntry
    from rag_search.daemon import sweeps

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

    import rag_search.daemon.federation as fed_mod
    import rag_search.embed.embedder as embed_mod
    import rag_search.index.indexer as indexer_mod
    import rag_search.index.store as store_mod
    import rag_search.kb.bpre as bpre_mod
    import rag_search.kb.wiki as wiki_mod
    from rag_search.core.config import index_dir
    from rag_search.daemon import sweeps
    from rag_search.graph import llm as llm_mod

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

    from rag_search.daemon import sweeps

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


def _write_tree(root, files: dict[str, str]) -> None:
    import os
    for rel, content in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)


def test_gitignore_respected_root_and_nested():
    """DIS1: root + nested .gitignore both drop matching files/dirs from iter_files."""
    import tempfile
    from pathlib import Path

    from rag_search.core.index_config import ProjectConfig
    from rag_search.index.discover import iter_files

    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, {
            "src/main.py": "print(1)\n",
            ".gitignore": "rootgen\n",
            "rootgen/out.txt": "x\n",
            "wiki/.gitignore": "genroot\n*.tmp\n",
            "wiki/genroot/bundle.js": "x\n",
            "wiki/app.tmp": "x\n",
            "wiki/index.html": "<html></html>\n",
        })
        got = {str(p.relative_to(tmp)) for p in iter_files(Path(tmp), cfg=ProjectConfig())}
        assert "src/main.py" in got and "wiki/index.html" in got
        assert not any(g.startswith("rootgen/") for g in got), "root .gitignore not honored"
        assert not any(g.startswith("wiki/genroot/") for g in got), "nested .gitignore not honored"
        assert "wiki/app.tmp" not in got, "nested .gitignore glob pattern not honored"


def test_hidden_dir_skip_tool_caches():
    """DIS2: hidden dirs (.svelte-kit, .playwright-mcp) are skipped by default, regardless
    of gitignore — this is the actual root-cause fixture (FINDING: Jul-1 idle-CPU burn)."""
    import tempfile
    from pathlib import Path

    from rag_search.core.index_config import ProjectConfig
    from rag_search.index.discover import iter_files

    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, {
            "src/main.py": "print(1)\n",
            ".svelte-kit/generated.js": "x\n",
            ".playwright-mcp/session.yml": "x\n",
        })
        got = {str(p.relative_to(tmp)) for p in iter_files(Path(tmp), cfg=ProjectConfig())}
        assert "src/main.py" in got
        assert not any(".svelte-kit" in g for g in got)
        assert not any(".playwright-mcp" in g for g in got)


def test_include_overrides_gitignore_exclude_beats_include():
    """DIS3: OSE config include re-keeps a gitignored path (config authoritative over
    .gitignore); exclude still beats include when both name the same path."""
    import tempfile
    from pathlib import Path

    from rag_search.core.index_config import ProjectConfig
    from rag_search.index.discover import iter_files

    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, {".gitignore": "rootgen\n", "rootgen/out.txt": "x\n"})
        root = Path(tmp)
        included = {
            str(p.relative_to(tmp))
            for p in iter_files(root, cfg=ProjectConfig(include=["rootgen/*"]))
        }
        assert "rootgen/out.txt" in included, "include must override .gitignore"

        both = {
            str(p.relative_to(tmp))
            for p in iter_files(
                root, cfg=ProjectConfig(include=["rootgen/*"], exclude=["rootgen/*"])
            )
        }
        assert not any(g.startswith("rootgen/") for g in both), "exclude must beat include"


def test_respect_gitignore_false_disables_gitignore_only():
    """DIS4: respect_gitignore=False re-admits gitignored paths but hidden-dir/IGNORED_DIRS
    default policy still applies (OSE config disabling gitignore is not a full opt-out)."""
    import tempfile
    from pathlib import Path

    from rag_search.core.index_config import ProjectConfig
    from rag_search.index.discover import iter_files

    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, {
            ".gitignore": "rootgen\n",
            "rootgen/out.txt": "x\n",
            ".svelte-kit/generated.js": "x\n",
        })
        got = {
            str(p.relative_to(tmp))
            for p in iter_files(Path(tmp), cfg=ProjectConfig(respect_gitignore=False))
        }
        assert "rootgen/out.txt" in got, "respect_gitignore=False must re-admit gitignored paths"
        assert not any(".svelte-kit" in g for g in got), "hidden-dir skip must still apply"


def test_drift_gate_quiescent_under_tool_cache_churn():
    """DIS5: the exact regression this fix targets — writing into a git-ignored,
    hidden tool-cache dir (.svelte-kit) must NOT change _source_fingerprint, so on_change
    does not retrigger the BPRE/enrich cascade for churn that isn't real source drift."""
    import tempfile

    from rag_search.daemon import sweeps

    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, {"src/main.py": "print(1)\n", ".svelte-kit/generated.js": "x\n"})
        sig1 = sweeps._source_fingerprint(tmp)
        sweeps._fingerprint_cache.pop(tmp, None)
        _write_tree(tmp, {".svelte-kit/generated.js": "x\nx\nx\n" * 50})
        sig2 = sweeps._source_fingerprint(tmp)
        assert sig1 == sig2, "tool-cache churn under a hidden dir must not flip the drift gate"


def test_is_ignored_path_agrees_with_iter_files():
    """DIS6: watcher (is_ignored_path) and indexer (iter_files) must agree on every path —
    they share the same _should_drop resolver so the drift gate and the watcher never diverge."""
    import tempfile
    from pathlib import Path

    from rag_search.core.index_config import ProjectConfig
    from rag_search.index.discover import is_ignored_path, iter_files

    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, {
            "src/main.py": "print(1)\n",
            ".svelte-kit/generated.js": "x\n",
            ".gitignore": "rootgen\n",
            "rootgen/out.txt": "x\n",
        })
        root = Path(tmp)
        cfg = ProjectConfig()
        kept = set(iter_files(root, cfg=cfg))
        for candidate in [
            root / "src" / "main.py",
            root / ".svelte-kit" / "generated.js",
            root / "rootgen" / "out.txt",
        ]:
            assert is_ignored_path(candidate, root, cfg) == (candidate not in kept), (
                f"is_ignored_path/iter_files disagree on {candidate}"
            )


# ─── Phase 5: BPRE code-only, discovery-unified reuse signature (BPS1-BPS4) ──────
# The 3rd idle-CPU root cause: bpre_source_sig was keyed on the all-files
# _source_fingerprint, so unrelated docs/hidden-dir churn flipped it and forced a
# full federation rebuild. These tests prove the code-only signature (_bpre_code_sig,
# routed through the same HR35 resolver as iter_files) is quiescent under that churn
# while still detecting real code drift.


@pytest.fixture
def _bps_fed():
    from tests.live._bpre_fixture import build_synth_federation, teardown_synth_federation
    fed = build_synth_federation()
    yield fed
    teardown_synth_federation(fed)


def _bps_no_llm(root: str) -> int:
    from rag_search.graph.llm import no_deepseek
    from rag_search.kb.bpre import reconstruct_processes
    with no_deepseek():
        return reconstruct_processes(root)


def _bps_spy_trace_processes():
    """Wrap bpre_mod._trace_processes with a call-counting spy; caller must restore."""
    import rag_search.kb.bpre as bpre_mod
    calls: list[int] = []
    orig = bpre_mod._trace_processes

    def _wrapped(*a, **k):
        calls.append(1)
        return orig(*a, **k)

    bpre_mod._trace_processes = _wrapped
    return calls, orig


def test_bps1_docs_churn_quiescent_no_rebuild(_bps_fed, caplog):
    """BPS1: touching a docs/*.yaml file inside a member must not change the code
    sig — reconstruct_processes reuses the stamped result (logs 'reusing
    stamp-matched'), it does not call _trace_processes again."""
    import logging
    import os
    import time

    import rag_search.kb.bpre as bpre_mod

    fed = _bps_fed
    docs_file = fed.cart + "/docs/notes.yaml"
    os.makedirs(os.path.dirname(docs_file), exist_ok=True)
    with open(docs_file, "w") as f:
        f.write("note: initial\n")
    bpre_mod._invalidate_bpre_code_sig(fed.cart)
    _bps_no_llm(fed.root)  # baseline build with the docs file already present

    calls, orig = _bps_spy_trace_processes()
    try:
        time.sleep(1.1)  # ensure a distinct mtime tick
        with open(docs_file, "w") as f:
            f.write("note: edited\n")
        bpre_mod._invalidate_bpre_code_sig(fed.cart)
        with caplog.at_level(logging.INFO, logger="rag_search.kb.bpre"):
            _bps_no_llm(fed.root)
        assert not calls, "docs-only churn must not trigger a rebuild"
        assert any("reusing stamp-matched" in r.message for r in caplog.records), (
            "docs-only churn must reuse the stamped result, not reconstruct"
        )
    finally:
        bpre_mod._trace_processes = orig


def test_bps2_hidden_dir_tool_cache_churn_no_rebuild(_bps_fed):
    """BPS2: touching a .claude/**/*.js tool-cache file (the live build_docauth_flow.js
    pattern) must not trigger a rebuild — hidden dirs are excluded by the same
    HR35 resolver _source_files now uses."""
    import os
    import time

    import rag_search.kb.bpre as bpre_mod

    fed = _bps_fed
    cache_file = fed.checkout + "/.claude/skills/tool/build.js"
    os.makedirs(os.path.dirname(cache_file), exist_ok=True)
    with open(cache_file, "w") as f:
        f.write("console.log('tool cache churn');\n")
    bpre_mod._invalidate_bpre_code_sig(fed.checkout)
    _bps_no_llm(fed.root)  # baseline build with the hidden-dir file already present

    calls, orig = _bps_spy_trace_processes()
    try:
        time.sleep(1.1)
        with open(cache_file, "w") as f:
            f.write("console.log('tool cache churn again');\n")
        bpre_mod._invalidate_bpre_code_sig(fed.checkout)
        _bps_no_llm(fed.root)
        assert not calls, "hidden-dir tool-cache churn must not trigger a rebuild"
    finally:
        bpre_mod._trace_processes = orig


def test_bps3_real_code_drift_still_rebuilds(_bps_fed):
    """BPS3: editing a real member .go service file must still flip the sig and
    trigger a rebuild — the code-only signature must not become inert."""
    import time

    import rag_search.kb.bpre as bpre_mod

    fed = _bps_fed
    _bps_no_llm(fed.root)  # baseline build

    calls, orig = _bps_spy_trace_processes()
    try:
        time.sleep(1.1)
        checkout_go = fed.checkout + "/checkout.go"
        with open(checkout_go, "a") as f:
            f.write("\n// drift comment\n")
        bpre_mod._invalidate_bpre_code_sig(fed.checkout)
        _bps_no_llm(fed.root)
        assert calls, "real member code drift must still trigger a rebuild"
    finally:
        bpre_mod._trace_processes = orig


def test_bps4_convergence_second_call_reuses(_bps_fed):
    """BPS4: two consecutive reconstruct_processes calls with no code change in
    between must converge — the second call reuses (stamp written at the end of
    the first rebuild equals the stamp read at the start of the second call)."""
    import rag_search.kb.bpre as bpre_mod

    fed = _bps_fed
    _bps_no_llm(fed.root)  # first call: builds and stamps

    calls, orig = _bps_spy_trace_processes()
    try:
        _bps_no_llm(fed.root)  # second call: no code change since first
        assert not calls, "second consecutive call with no code change must reuse, not rebuild"
    finally:
        bpre_mod._trace_processes = orig


def _wt_wait_for(pred, timeout: float = 6.0, step: float = 0.05) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


def test_wt1_ignored_dir_churn_never_reaches_on_change():
    """WT1 (Phase 6): a burst of writes into a hidden dir + a gitignored dir must
    never invoke on_change — the exact 4th-root-cause regression (watchdog used to
    deliver every raw event to Python before any gate could say "no drift")."""
    import tempfile
    import time
    from pathlib import Path

    from rag_search.daemon.watcher import Watcher

    calls: list[tuple[str, list[Path]]] = []
    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, {
            "src/main.py": "print(1)\n",
            ".gitignore": "cache/\n",
        })
        w = Watcher(on_change=lambda root, files: calls.append((root, files)))
        w.watch(tmp)
        w.start()
        try:
            time.sleep(0.3)  # let the watcher thread start observing
            for i in range(20):
                _write_tree(tmp, {f".svelte-kit/gen_{i}.js": "x\n"})
                _write_tree(tmp, {f"cache/tmp_{i}.txt": "x\n"})
            # No predicate to wait on (we're proving absence) — a fixed settle window
            # longer than watchfiles' default debounce (1600ms) is the correct check.
            time.sleep(3.0)
            assert not calls, f"ignored-dir churn must never reach on_change; got {calls}"
        finally:
            w.stop()


def test_wt2_real_edit_fires_once():
    """WT2 (Phase 6): editing a tracked source file yields exactly one on_change
    call for its root, carrying that file."""
    import tempfile
    import time
    from pathlib import Path

    from rag_search.daemon.watcher import Watcher

    calls: list[tuple[str, list[Path]]] = []
    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, {"src/main.py": "print(1)\n"})
        w = Watcher(on_change=lambda root, files: calls.append((root, files)))
        w.watch(tmp)
        w.start()
        try:
            time.sleep(0.3)
            target = Path(tmp) / "src" / "main.py"
            with open(target, "a") as f:
                f.write("print(2)\n")
            assert _wt_wait_for(lambda: len(calls) >= 1), f"real edit must fire on_change; got {calls}"
            assert len(calls) == 1, f"expected exactly one on_change call; got {calls}"
            root, files = calls[0]
            assert root == tmp
            assert any(p.name == "main.py" for p in files)
        finally:
            w.stop()


def test_wt3_batch_coalescing_single_call_per_burst():
    """WT3 (Phase 6): N writes to one tracked file within a debounce window must
    yield a single on_change for that root — Rust-side coalescing subsumes the old
    hand-rolled per-project debounce throttle."""
    import tempfile
    import time
    from pathlib import Path

    from rag_search.daemon.watcher import Watcher

    calls: list[tuple[str, list[Path]]] = []
    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, {"src/main.py": "print(1)\n"})
        w = Watcher(on_change=lambda root, files: calls.append((root, files)))
        w.watch(tmp)
        w.start()
        try:
            time.sleep(0.3)
            target = Path(tmp) / "src" / "main.py"
            # Tight loop, no inter-write sleep: keeps every write well inside the
            # 50ms `step` window watchfiles uses to decide a burst has ended, so
            # the whole burst coalesces into one batch (matches live storm shape).
            for i in range(10):
                with open(target, "a") as f:
                    f.write(f"print({i})\n")
            assert _wt_wait_for(lambda: len(calls) >= 1), f"burst must fire on_change; got {calls}"
            time.sleep(2.0)  # settle window past debounce, to catch any extra calls
            assert len(calls) == 1, f"burst of 10 writes must coalesce to 1 on_change call; got {calls}"
        finally:
            w.stop()


def test_wt4_dynamic_add_restart_delivers_new_root():
    """WT4 (Phase 6): watch(new_root) while the loop is already running must relaunch
    and deliver the new root's edits, without dropping the original root's events."""
    import tempfile
    import time
    from pathlib import Path

    from rag_search.daemon.watcher import Watcher

    calls: list[tuple[str, list[Path]]] = []
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        _write_tree(tmp1, {"src/a.py": "print(1)\n"})
        _write_tree(tmp2, {"src/b.py": "print(1)\n"})
        w = Watcher(on_change=lambda root, files: calls.append((root, files)))
        w.watch(tmp1)
        w.start()
        try:
            time.sleep(0.3)  # loop is now running with only tmp1
            w.watch(tmp2)  # dynamic add while running — must trigger a restart

            with open(Path(tmp2) / "src" / "b.py", "a") as f:
                f.write("print(2)\n")
            assert _wt_wait_for(lambda: any(root == tmp2 for root, _ in calls)), (
                f"dynamically added root's edits must be delivered; got {calls}"
            )

            with open(Path(tmp1) / "src" / "a.py", "a") as f:
                f.write("print(2)\n")
            assert _wt_wait_for(lambda: any(root == tmp1 for root, _ in calls)), (
                f"original root's events must not be dropped after a dynamic add; got {calls}"
            )
        finally:
            w.stop()
