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
    """
    from rag_search.daemon import sweeps

    was = sweeps._PAUSED
    sweeps._PAUSED = paused
    try:
        yield
    finally:
        sweeps._PAUSED = was
