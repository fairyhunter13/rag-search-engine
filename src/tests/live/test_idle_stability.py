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


# FP2 and FP2b are gone with tier 3: both gated the _BPRE_CASCADE_DEBOUNCE_S window around
# _regen_owning_processes / _regen_owning_federations, and a member's owning federation has no
# wiki or BPRE left to regenerate. Nothing cascades off a member edit any more, so there is no
# debounce to assert about — the absence itself is covered by DK2's tree-wide no-LLM guard.


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
    """IS2: no stale rse-test-dirs entries may be enabled in the live registry.

    The current session's sample_workspace paths are excluded — they are
    legitimately registered for the duration of the test session and torn down
    at session end.  Only entries from previous (leaked) sessions are flagged.
    Worktrees exclusion is config-driven (RSE_FEDERATION_EXCLUDE), not hardcoded.
    """
    from rag_search.core.registry import list_projects
    from tests.live._projects import sample_project_paths

    current_session_paths = sample_project_paths(sample_workspace)
    junk = [
        e.path for e in list_projects()
        if e.enabled and "/rse-test-dirs/" in e.path and e.path not in current_session_paths
    ]
    assert not junk, (
        f"{len(junk)} junk entries still enabled: {junk[:3]!r}. Prune and restart the daemon."
    )


def _settle(sweeps) -> None:
    """Wait out the graph lane, which runs on_change's heavy half off the caller's thread.

    Two reasons, and the second is the load-bearing one. The gates below assert on _label_project
    calls, and without this they would be assertions about scheduling rather than about the drift
    gate. More importantly the fixtures restore the real _label_project in `finally`, so a pass
    that landed after teardown would run it against a TemporaryDirectory that no longer exists.
    """
    assert sweeps._graph_lane_join(timeout=180.0), "graph lane did not finish its pass"


def test_drift_gate_skips_labelling_when_sig_unchanged():
    """FP5: on_change must not call _label_project when source fingerprint is unchanged."""
    import os
    import tempfile

    from rag_search.daemon import sweeps

    calls: list[str] = []
    orig_label, orig_idx = sweeps._label_project, sweeps._index_files
    with tempfile.TemporaryDirectory() as tmp:
        sig = sweeps._code_source_fingerprint(tmp)
        sweeps._last_labelled_sig[tmp] = sig
        sweeps._last_index_fail.pop(tmp, None)
        sweeps._last_lane_run.pop(tmp, None)
        sweeps._label_project = calls.append  # type: ignore[assignment]
        sweeps._index_files = lambda *a, **kw: None  # type: ignore[assignment]
        try:
            sweeps.on_change(tmp, [os.path.join(tmp, "app.log")])
            _settle(sweeps)  # else "no pass yet" and "no pass ever" look identical
            assert not calls, f"drift gate must suppress labelling when sig unchanged; calls={calls}"
        finally:
            sweeps._label_project, sweeps._index_files = orig_label, orig_idx
            sweeps._last_labelled_sig.pop(tmp, None)
            sweeps._last_lane_run.pop(tmp, None)


def test_drift_gate_triggers_labelling_when_sig_changes():
    """FP6: on_change must call _label_project when source fingerprint differs."""
    import tempfile

    from rag_search.daemon import sweeps

    calls: list[str] = []
    orig_label, orig_idx = sweeps._label_project, sweeps._index_files
    with tempfile.TemporaryDirectory() as tmp:
        sweeps._last_labelled_sig[tmp] = "stale-sig-will-never-match"
        sweeps._last_index_fail.pop(tmp, None)
        sweeps._last_lane_run.pop(tmp, None)
        sweeps._label_project = lambda p: calls.append(p)  # type: ignore[assignment]
        sweeps._index_files = lambda *a, **kw: None  # type: ignore[assignment]
        try:
            sweeps.on_change(tmp, [tmp + "/main.go"])
            _settle(sweeps)
            assert calls, f"labelling must fire when sig differs; calls={calls}"
            assert sweeps._last_labelled_sig.get(tmp) != "stale-sig-will-never-match"
        finally:
            sweeps._label_project, sweeps._index_files = orig_label, orig_idx
            sweeps._last_labelled_sig.pop(tmp, None)
            sweeps._last_lane_run.pop(tmp, None)


# ─── HR38: on_change's code-only cascade gate ──────────────────────────────────────────────
# The 4th idle-CPU root cause the code-only fingerprint closes: on_change's own drift gate
# was keyed on the all-files _source_fingerprint, so docs/config/image churn kept waking the
# graph lane for churn that changes no source. With tier 3 deleted these are the only surviving
# gates on that fingerprint, so they carry the whole class on their own.


@pytest.fixture
def _fcg_project():
    """A real tmp project, one code file, _label_project/_index_files stubbed to a call list."""
    import tempfile

    from rag_search.daemon import sweeps

    with tempfile.TemporaryDirectory() as tmp:
        _write_tree(tmp, {"main.py": "def f():\n    pass\n"})
        calls: list[str] = []
        orig_label, orig_idx = sweeps._label_project, sweeps._index_files
        sweeps._label_project = calls.append  # type: ignore[assignment]
        sweeps._index_files = lambda *a, **kw: None  # type: ignore[assignment]
        sweeps._last_index_fail.pop(tmp, None)
        sweeps._last_lane_run.pop(tmp, None)
        sweeps._last_labelled_sig.pop(tmp, None)
        try:
            sweeps.on_change(tmp, [tmp + "/main.py"])  # baseline stamp
            _settle(sweeps)
            assert calls == [tmp], "baseline on_change must label exactly once"
            yield tmp, calls, sweeps
        finally:
            # Unasserted on purpose: a test that already failed must report its own reason, not a
            # join timeout. Still required — restoring the real _label_project under a running
            # lane pass points it at a directory this `with` block is about to delete.
            sweeps._graph_lane_join(timeout=180.0)
            sweeps._label_project, sweeps._index_files = orig_label, orig_idx
            sweeps._last_labelled_sig.pop(tmp, None)
            sweeps._last_lane_run.pop(tmp, None)
            sweeps._last_index_fail.pop(tmp, None)


def test_fcg1_docs_wiki_churn_quiescent(_fcg_project):
    """FCG1: editing docs/*.md + generated wiki/*.md after the baseline must not re-label."""
    tmp, calls, sweeps = _fcg_project
    sweeps._last_lane_run.pop(tmp, None)  # bypass the 45s debounce for this scenario
    _write_tree(tmp, {"docs/notes.md": "hello\n", "wiki/L1_overview.md": "generated\n"})
    sweeps.on_change(tmp, [tmp + "/docs/notes.md", tmp + "/wiki/L1_overview.md"])
    _settle(sweeps)
    assert calls == [tmp], f"docs/wiki-only churn must not re-trigger the cascade; calls={calls}"


def test_fcg2_config_image_churn_quiescent(_fcg_project):
    """FCG2: editing config (*.json) + image (*.png) after the baseline must not re-label."""
    tmp, calls, sweeps = _fcg_project
    sweeps._last_lane_run.pop(tmp, None)
    _write_tree(tmp, {"config/settings.json": '{"a": 1}\n', "assets/logo.png": "not-a-png\n"})
    sweeps.on_change(tmp, [tmp + "/config/settings.json", tmp + "/assets/logo.png"])
    _settle(sweeps)
    assert calls == [tmp], f"config/image-only churn must not re-trigger the cascade; calls={calls}"


def test_fcg3_real_code_drift_fires_cascade_once(_fcg_project):
    """FCG3: editing main.py after the baseline must re-label exactly once — gate not inert."""
    import time
    tmp, calls, sweeps = _fcg_project
    sweeps._last_lane_run.pop(tmp, None)
    time.sleep(1.1)  # ensure a distinct mtime tick (sig truncates to whole seconds)
    _write_tree(tmp, {"main.py": "def f():\n    pass\n\ndef g():\n    pass\n"})
    sweeps.on_change(tmp, [tmp + "/main.py"])
    _settle(sweeps)
    assert calls == [tmp, tmp], f"real code drift must re-trigger the cascade once; calls={calls}"


def test_fcg4_convergence_second_call_reuses(_fcg_project):
    """FCG4: a second consecutive on_change with no change since baseline must not re-label."""
    tmp, calls, sweeps = _fcg_project
    sweeps._last_lane_run.pop(tmp, None)  # bypass debounce so only the sig gate is exercised
    sweeps.on_change(tmp, [tmp + "/main.py"])
    _settle(sweeps)
    assert calls == [tmp], f"unchanged second call must reuse, not re-label; calls={calls}"


def test_watcher_prefers_inotify_over_poll():
    """IS3: Watcher.start() runs one watchfiles (Rust notify) thread — no hand-rolled poll loop."""
    from rag_search.daemon.watcher import Watcher

    w = Watcher(on_change=lambda p, f: None)
    w.start()
    try:
        assert w._thread is not None and w._thread.is_alive(), (
            "watcher thread must be running"
        )
        assert w._thread.name == "rse-watcher", "single unified watchfiles thread expected"
    finally:
        w.stop()
    assert not w._thread.is_alive(), "watcher thread must stop cleanly"


# FP7/FP8/FP9 are gone with tier 3, and FP7 is the one worth explaining. It asserted that
# reconcile_projects() sets and always clears _reconcile_active — a flag whose sole reader was
# _enrich_project's BPRE bulk-suppression gate. With the fan-out deleted the flag had no reader
# left, so it was deleted from sweeps.py in this same commit rather than kept under test: a
# lifecycle gate on state nothing consults proves only that the gate still runs.
# FP8 (the reconcile root-pass calls reconstruct_processes every pass) and FP9 (that same call
# is suppressed during a bulk pass) were both assertions about BPRE, which no longer exists.


def test_heavy_lock_serializes_concurrent_passes():
    """FP10: _HEAVY_LOCK must allow at most one CPU-bound graph pass at a time across
    threads (Part D2) — caps daemon CPU at ~one core instead of pinning two concurrently."""
    import threading
    import time

    from rag_search.daemon import sweeps

    concurrent = 0
    max_concurrent = 0
    counter_lock = threading.Lock()  # guards the counter itself, not the thing under test

    def _hold():
        nonlocal concurrent, max_concurrent
        with sweeps._HEAVY_LOCK:
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
    assert max_concurrent == 1, f"_HEAVY_LOCK must serialize passes; max_concurrent={max_concurrent}"


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
    """DIS3: RSE config include re-keeps a gitignored path (config authoritative over
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
    default policy still applies (RSE config disabling gitignore is not a full opt-out)."""
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
    does not retrigger the graph-lane pass for churn that isn't real source drift."""
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
    import os
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
            # The size rule is the one these two screens disagreed on in production: it lived in
            # iter_files alone, so the watcher indexed a file past the cap and the drift check
            # then purged it, every write. Measured on inosoft's 143-172 kB diagram specs.
            "spec/oversize.yaml": "a: 1\n" * 30_000,   # ~180 kB, data cap is 100 kB
            "spec/under.yaml": "a: 1\n",
            "src/empty.py": "",                        # zero bytes is also a discovery rule
            # The text screen, which asks the bytes rather than the extension. All three files
            # are needed: the .png pair is what distinguishes the shipped rule from an extension
            # list, and the .py is what proves a known language never pays the read.
            "assets/image-1.png": "\x00" + "\xd9g\xc3E_\x16" * 20,
            "assets/notes.png": "misnamed, but plainly text\n",
            "src/nulled.py": "x = 1\n\x00\n",
        })
        root = Path(tmp)
        cfg = ProjectConfig()
        kept = set(iter_files(root, cfg=cfg))
        # Derived, not hand-listed. The previous three-name list could not see a rule its own
        # fixtures never triggered, which is how the size disagreement survived this gate.
        candidates = [Path(dp) / f for dp, _d, fs in os.walk(root) for f in fs]
        assert len(candidates) >= 10, f"fixture tree did not materialise: {candidates}"
        # Agreement alone is satisfied by deleting the rule from both screens, so pin the rule.
        assert root / "spec" / "oversize.yaml" not in kept, "size cap not applied"
        assert root / "spec" / "under.yaml" in kept, "size cap swallowed a file under the cap"
        assert root / "assets" / "image-1.png" not in kept, "non-text file was not screened out"
        assert root / "assets" / "notes.png" in kept, "text screen keyed on the name, not the bytes"
        assert root / "src" / "nulled.py" in kept, "text screen ran on a known-language file"
        for candidate in candidates:
            assert is_ignored_path(candidate, root, cfg) == (candidate not in kept), (
                f"is_ignored_path/iter_files disagree on {candidate}"
            )


# BPS1-BPS4 are gone with tier 3. They gated _bpre_code_sig: proving a code-only reuse signature
# stayed quiescent under docs/hidden-dir churn while still catching real code drift. That defect
# class did not leave with BPRE — the same shape lives on in _code_source_fingerprint, and FCG1-FCG4
# above are its surviving gates, over the graph lane instead of a federation rebuild.


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


def test_wt5_prefix_sibling_roots_no_misattribution():
    """WT5: root name that is a string-prefix of a sibling's (`foo` vs `foo-bar`) must
    never misattribute events — raw `str.startswith` has no path boundary check."""
    import tempfile
    import time
    from pathlib import Path

    from rag_search.daemon.watcher import Watcher

    calls: list[tuple[str, list[Path]]] = []
    with tempfile.TemporaryDirectory() as base:
        short, long_ = str(Path(base) / "foo"), str(Path(base) / "foo-bar")
        _write_tree(short, {"src/a.py": "print(1)\n"})
        _write_tree(long_, {"src/b.py": "print(1)\n"})
        w = Watcher(on_change=lambda root, files: calls.append((root, files)))
        w.watch(short)
        w.watch(long_)
        w.start()
        try:
            time.sleep(0.3)
            with open(Path(long_) / "src" / "b.py", "a") as f:
                f.write("print(2)\n")
            assert _wt_wait_for(lambda: any(r == long_ for r, _ in calls)), calls
            assert not any(r == short for r, _ in calls), f"misattributed to foo: {calls}"
        finally:
            w.stop()
