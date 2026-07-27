"""Live proof gates: the inotify reader must never run the work (no mocks).

`Watcher._loop` called `self._on_change(root, files)` synchronously, inside the one `rse-watcher`
thread that is also the sole consumer of the `watchfiles` generator. Everything `on_change` reaches
rode that thread: tree-sitter extraction through a 1-worker IPC pool, a 194-member BPRE federation
scan, and DeepSeek network calls. py-spy caught it parked there for 15+ minutes, during which no
project on this box saw a file event at all.

Nothing about the *output* discriminates: the same callbacks fire with the same file lists either
way. Only the timing differs, so every gate here is a liveness or timing assertion — the trap named
in the guard-tests-must-discriminate lesson. `Watcher` takes `on_change` as a constructor argument,
so each gate injects a real callable and drives a real inotify watch over a real directory.

HL1  an event for B must be handled while A's callback is still blocked (head-of-line)
HL2  events arriving for A while A is in flight are coalesced into one follow-up, not dropped
HL3  a project's callback is never entered concurrently with itself
HL4  stop() returns promptly even with a callback blocked
HL5  the dispatch loop must wait without a timeout — notification-driven, never a poll clock
"""
from __future__ import annotations

import threading
import time

import pytest

pytestmark = pytest.mark.live

_SETTLE_S = 8.0  # generous: watchfiles' default debounce is 1.6s and this box is often busy


class _Recorder:
    """Real (never mocked) on_change callable that blocks for `hold_s` on `slow_root`."""

    def __init__(self, slow_root: str = "", hold_s: float = 0.0) -> None:
        self.slow_root, self.hold_s = slow_root, hold_s
        self.mu = threading.Lock()
        self.starts: list[tuple[str, float]] = []            # (root, monotonic) per entry
        self.batches: list[tuple[str, frozenset[str]]] = []   # (root, file names) per entry
        self.active: dict[str, int] = {}                     # root -> concurrent entries
        self.max_active: dict[str, int] = {}
        self.released = threading.Event()

    def __call__(self, root: str, files: list) -> None:
        with self.mu:
            self.starts.append((root, time.monotonic()))
            self.batches.append((root, frozenset(f.name for f in files)))
            n = self.active.get(root, 0) + 1
            self.active[root] = n
            self.max_active[root] = max(self.max_active.get(root, 0), n)
        try:
            if root == self.slow_root and self.hold_s:
                self.released.wait(timeout=self.hold_s)
        finally:
            with self.mu:
                self.active[root] -= 1

    def roots(self) -> list[str]:
        with self.mu:
            return [r for r, _ in self.starts]

    def first_start(self, root: str) -> float | None:
        with self.mu:
            return next((t for r, t in self.starts if r == root), None)


def _start(rec, *roots):
    from rag_search.daemon.watcher import Watcher
    w = Watcher(rec)
    for r in roots:
        w.watch(str(r))
    w.start()
    time.sleep(1.5)  # let the rust watch arm before the first write
    return w


def _wait_for(pred, timeout: float = _SETTLE_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture()
def two_roots(safe_tmp_path):
    a, b = safe_tmp_path / "proj_a", safe_tmp_path / "proj_b"
    for d in (a, b):
        d.mkdir()
        (d / "seed.py").write_text("def seed():\n    return 1\n")
    return str(a), str(b)


def test_hl1_a_slow_project_must_not_block_every_other_project(two_roots):
    """HL1: while A's callback is blocked, an edit in B must still be handled.

    This is the whole defect. `_loop` is the only consumer of the watchfiles generator, so any time
    it spends inside `_on_change` is time no *other* project's events are read. Live evidence: the
    watcher thread sat in a 194-member BPRE scan for 15+ minutes and the entire box went blind.

    Output cannot tell the two designs apart — B's callback fires either way, with the same file
    list. Only when it fires differs, so the assertion has to be about elapsed time.
    """
    a, b = two_roots
    rec = _Recorder(slow_root=a, hold_s=20.0)
    w = _start(rec, a, b)
    try:
        from pathlib import Path
        Path(a, "hot.py").write_text("def a_one():\n    return 1\n")
        assert _wait_for(lambda: a in rec.roots()), "HL1: A's event never arrived — setup is broken"
        t_a = rec.first_start(a)

        time.sleep(0.5)  # A is now parked inside the callback
        Path(b, "hot.py").write_text("def b_one():\n    return 2\n")
        assert _wait_for(lambda: b in rec.roots()), (
            "HL1: B's event never reached on_change while A's callback was blocked — the inotify "
            "reader is still running the work inline"
        )
        lag = rec.first_start(b) - t_a
        assert lag < 8.0, (
            f"HL1: B waited {lag:.1f}s behind A's 20s callback — head-of-line blocking, not dispatch"
        )
    finally:
        rec.released.set()
        w.stop(timeout=10.0)


def test_hl2_events_during_a_pass_are_coalesced_not_dropped_or_queued(two_roots):
    """HL2: edits landing while A is in flight must reach the *next* pass, exactly once.

    Two failure modes bracket the right answer and neither shows up in a plain "did the callback
    fire" check. Dropping the events loses work (the same class of bug as the debounce drop WG7
    covers). Queueing each batch separately turns an editor's save storm into N full KB passes.
    The union of the pending files, delivered once, is the only shape that is both.

    Honest status: this one is **green before the fix too**, and that is not a claim the gate is
    weak — it is what the gate is for. Today the reader is blocked, so the three edits pile up in
    the kernel's inotify queue and arrive as one batch by accident. Once the reader keeps reading
    they arrive as three separate enqueues, and only an explicit coalescing map still passes. HL2
    fails against a `queue.Queue` dispatch, which is the realistic wrong turn here; it cannot fail
    against a design that has no dispatch at all.
    """
    from pathlib import Path
    a, _b = two_roots
    rec = _Recorder(slow_root=a, hold_s=6.0)
    w = _start(rec, a)
    try:
        Path(a, "one.py").write_text("def one():\n    return 1\n")
        assert _wait_for(lambda: a in rec.roots()), "HL2: A's first event never arrived"
        for i, name in enumerate(("two.py", "three.py", "four.py")):
            Path(a, name).write_text(f"def f{i}():\n    return {i}\n")
            time.sleep(0.4)
        rec.released.set()

        assert _wait_for(lambda: len(rec.roots()) >= 2), "HL2: the pending edits were dropped"
        time.sleep(3.0)  # let any extra passes show up before counting
        with rec.mu:
            follow = [names for r, names in rec.batches[1:] if r == a]
        assert len(follow) == 1, f"HL2: expected one coalesced follow-up pass, got {len(follow)}"
        assert {"two.py", "three.py", "four.py"} <= set(follow[0]), (
            f"HL2: follow-up carried {sorted(follow[0])} — files reported mid-pass were lost"
        )
    finally:
        rec.released.set()
        w.stop(timeout=10.0)


def test_hl3_one_project_is_never_processed_by_two_workers_at_once(two_roots):
    """HL3: dispatching concurrently must not let a project's pass overlap itself.

    `on_change` keeps per-project module state (`_last_kb_enrich`, `_last_enriched_sig`,
    `_pending_graph_files`) that assumes one pass per project at a time; two overlapping passes
    would interleave the read-modify-write of the pending file set. Affinity is what lets
    sweeps.py stay untouched, so it is a gate, not an implementation detail.

    Same honest status as HL2: trivially green today, because one thread cannot overlap itself.
    It discriminates against the *next* mistake — dispatching by batch instead of by project,
    which is the natural thing to write and would corrupt `_pending_graph_files` silently.
    """
    from pathlib import Path
    a, _b = two_roots
    rec = _Recorder(slow_root=a, hold_s=5.0)
    w = _start(rec, a)
    try:
        for i, name in enumerate(("p1.py", "p2.py", "p3.py")):
            Path(a, name).write_text(f"def g{i}():\n    return {i}\n")
            time.sleep(1.2)  # spread past the debounce so each is its own batch
        assert _wait_for(lambda: a in rec.roots(), timeout=12.0), "HL3: no event arrived"
        rec.released.set()
        time.sleep(4.0)
        with rec.mu:
            peak = rec.max_active.get(a, 0)
        assert peak == 1, f"HL3: {peak} concurrent passes for one project — affinity is broken"
    finally:
        rec.released.set()
        w.stop(timeout=10.0)


def test_hl4_stop_returns_promptly_while_a_callback_is_blocked(two_roots):
    """HL4: shutdown must not wait out the work in flight.

    Today `stop_event` is only consulted between batches, so a `stop()` issued during a 12-minute
    BPRE pass returns only when that pass ends — on daemon restart systemd then SIGKILLs, which is
    how parse workers got orphaned in the first place. Correct behaviour: the reader and the
    dispatch loop unblock immediately; only the in-flight worker is allowed to run long.
    """
    from pathlib import Path
    a, _b = two_roots
    rec = _Recorder(slow_root=a, hold_s=30.0)
    w = _start(rec, a)
    try:
        Path(a, "hot.py").write_text("def h():\n    return 1\n")
        assert _wait_for(lambda: a in rec.roots()), "HL4: the blocking callback never started"
        w.stop(timeout=5.0)
        # `stop()` joins *with a timeout*, so it returns on schedule even when the thread is still
        # wedged — measuring how long stop() took would pass vacuously. Ask the reader if it died.
        assert not w._thread.is_alive(), (
            "HL4: the reader thread outlived stop() — shutdown is queued behind the 30s callback, "
            "so systemd's SIGKILL lands on a live pass (this is how parse workers got orphaned)"
        )
    finally:
        rec.released.set()


def test_hl5_dispatch_workers_wait_without_a_timeout():
    """HL5: the dispatch loop must block on a notification, never on a clock.

    A `wait(timeout=...)` here would be a poll loop wearing a Condition's clothes: it would still
    pass HL1-HL4 while waking N threads on a fixed interval forever, exactly the regression
    `test_no_fixed_interval_timers` exists to stop. Output cannot see the difference, so the gate
    reads the source — the same shape that doctrine check already uses.
    """
    import ast
    import inspect

    from rag_search.daemon import watcher as mod

    tree = ast.parse(inspect.getsource(mod))
    workers = [
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and "worker" in n.name
    ]
    # Without this the gate passes vacuously on a Watcher that has no dispatch layer at all.
    assert workers, "HL5: no dispatch worker in watcher.py — the reader still runs the work inline"
    offenders = []
    for node in workers:
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "wait"
                and (call.args or call.keywords)
            ):
                offenders.append(f"{node.name}:{call.lineno}")
    assert not offenders, (
        f"HL5: dispatch wait() carries a timeout at {offenders} — that is a poll clock; the "
        "watcher must stay purely filesystem-notification driven"
    )
