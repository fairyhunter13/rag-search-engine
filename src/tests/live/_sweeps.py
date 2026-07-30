"""Save/restore helpers for the daemon's sweep-pause state.

`daemon/sweeps.py:10` `_PAUSED` is a bare global with no nesting or ownership, and the live
suite's session-scoped autouse `pause_sweeps` fixture makes "paused" load-bearing for the
whole run: the daemon must not compete for the GPU with the ~8.4 GB embedder + reranker the
suite loads in-process (HR41), nor reconcile-index the suite's own temp projects behind tests
that assert un-indexed state.

So a test that hardcodes its restore ("...then resume") is asserting something about its
environment rather than putting back what it found. `test_cpu_budget.py`'s CB3 did exactly
that, and because it sorts 7th of 76 live files, ~70 files after it ran against a sweeping
daemon. The failure never names sweeps or the GPU — it surfaces as flakiness somewhere else
entirely, which is why it stayed latent behind CB3's `@slow` deselection.

Both helpers below read the state they are about to change and put that value back.
"""
from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator

import requests

_DAEMON = "http://127.0.0.1:8765"


@contextlib.contextmanager
def sweeps_state(paused: bool, *, base: str = _DAEMON) -> Iterator[dict]:
    """Set the *daemon's* sweep state for the block, then restore what it actually was.

    Yields the route's response body so a caller can assert on it. Nests correctly: each
    level restores the state its own entry observed.
    """
    body = _post(base, "pause" if paused else "resume")
    try:
        yield body
    finally:
        # Restore what entry found, not the inverse of what we set — an inner block may have
        # been a no-op (already in the requested state), in which case there is nothing to undo.
        _post(base, "pause" if body["previously_paused"] else "resume")


def renew_pause_lease(*, base: str = _DAEMON) -> None:
    """Re-arm the daemon's pause lease if it is paused, so a long run is never cut off mid-flight.

    The pause now expires (`sweeps._PAUSE_DEADLINE`), which is what stops a killed session wedging
    every sweep for hours — but the live suite runs far longer than one lease, and an expiry
    mid-run resumes exactly the sweeps `pause_sweeps` exists to stop: GPU contention with the
    in-process embedder, and reconcile indexing the suite's own temp projects behind tests that
    assert un-indexed state. Called after every test, so the lease only ever has to outlive a
    single test rather than the whole session.

    Conditional on the daemon reporting itself paused, and never the reverse: pausing a sweeping
    daemon here would be the clobber `sweeps_state` was written to prevent. Best-effort — a renewal
    that cannot reach the daemon must not fail the test that just passed.

    Keyed on `sweeps_pause_lease_s`, not `sweeps_paused_s`: the latter is elapsed time rounded to one
    decimal, so a pause set less than 50 ms ago reports 0.0 — the field meaning "paused" and the
    value meaning "not paused" collide. Lease-remaining is positive exactly while a lease is live,
    which is the thing being renewed. A lease already at 0.0 is deliberately *not* re-armed: the
    mechanism has decided to resume, and reinstating it from here would race that decision and hide
    the one signal that says a 30-minute pause went unrenewed.
    """
    with contextlib.suppress(Exception):
        health = requests.get(f"{base}/healthz", timeout=5).json()
        if health.get("sweeps_pause_lease_s", 0.0) > 0.0:
            _post(base, "pause")


_last_uptime_s: float | None = None
_uptime_lock = threading.Lock()


def renew_or_repair_lease(*, base: str = _DAEMON) -> None:
    """Renew the lease, or re-take it outright if the daemon restarted since the last call.

    The one case `renew_pause_lease`'s one-way rule must not cover, which is why this lives beside
    it: `_PAUSED`/`_PAUSE_DEADLINE` are module globals, so a restart destroys the lease with no
    refusal and no log — and because the survivor then reports 0.0, the rule above declines to
    re-arm it for the rest of the run. A restart is not "the mechanism decided to resume"; it is a
    different process that never knew a lease existed, so re-pausing puts back what was lost rather
    than racing a decision. The unconditional `_post` here is exactly that difference.

    `uptime_s` is the witness and it costs nothing new: `time.monotonic() - _START` can only
    *decrease* across a process boundary. A pause epoch would have been no better — it would be a
    module global too, so a restart resets it as well.

    Not hypothetical and not rare: `test_api_reload_returns_reloading` restarts the daemon on
    purpose and `live-fast` collects it, so before this every run spent everything after that test
    competing with reconcile and label sweeps for the same card.
    """
    global _last_uptime_s
    uptime = None
    with contextlib.suppress(Exception):
        uptime = float(requests.get(f"{base}/healthz", timeout=5).json()["uptime_s"])
    with _uptime_lock:
        restarted = uptime is not None and _last_uptime_s is not None and uptime < _last_uptime_s
        if uptime is not None:
            _last_uptime_s = uptime
    if restarted:
        with contextlib.suppress(Exception):
            _post(base, "pause")
    renew_pause_lease(base=base)


def _post(base: str, route: str) -> dict:
    r = requests.post(f"{base}/api/sweeps/{route}", timeout=5)
    r.raise_for_status()
    body = r.json()
    assert "previously_paused" in body, (
        f"POST /api/sweeps/{route} returned {body} with no 'previously_paused' — the running "
        "daemon predates that field, so the state to restore cannot be read. Restart it: "
        "`systemctl --user restart rag-search-mcp-daemon`."
    )
    return body


@contextlib.contextmanager
def local_sweeps_paused(paused: bool) -> Iterator[None]:
    """In-process twin of `sweeps_state`: the *test process's* own `sweeps._PAUSED`.

    Tests that call `reconcile_projects()` directly are driving their own imported module, not
    the daemon's process, so the HTTP routes cannot reach them. Same discipline regardless:
    restore what was read, never a constant.

    Goes through `set_paused` rather than assigning the flag, so the in-process twin exercises the
    same lease the daemon does — a bare assignment leaves `_PAUSE_DEADLINE` at None, and a pause
    with no deadline is the un-leased pause this replaced. `_PAUSED` is still read directly to
    learn what to restore: that read is the one thing `is_paused()` cannot answer, since it would
    resume an expired pause as a side effect of asking about it.
    """
    from rag_search.daemon import sweeps

    was = sweeps._PAUSED
    sweeps.set_paused(paused)
    try:
        yield
    finally:
        sweeps.set_paused(was)
