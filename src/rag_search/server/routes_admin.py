"""Root redirect, and the watcher's own view of what it is watching."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse


async def _root(request: Request) -> RedirectResponse:
    return RedirectResponse("/dashboard")


def _watcher_sync() -> tuple[dict, int]:
    """The armed root set, the pending queue, and what the registry says it should be.

    Both sides are returned because the interesting failure is the *difference*: roots armed
    for projects since disabled, and enabled projects never armed. Answering that used to
    require a py-spy dump of the reader thread's locals.
    """
    from rag_search.daemon.server import get_watcher, watch_roots

    watcher = get_watcher()
    if watcher is None:
        return {"running": False}, 503
    status = watcher.status()
    expected = sorted(watch_roots())
    armed = set(status.get("roots") or [])
    status["running"] = True
    status["expected"] = expected
    status["missing"] = sorted(set(expected) - armed)   # enabled, registered, never armed
    status["extra"] = sorted(armed - set(expected))     # armed for something now disabled
    return status, 200


async def _watcher(request: Request) -> JSONResponse:
    """Off the event loop, like every other blocking route in this server.

    This one was the exception, and the body above is not the cheap dict read it looks like.
    `watch_roots()` calls `list_projects()`, which reads the registry off disk and takes its
    cross-process file lock when a migration is owed, then `is_federation_excluded()` resolves
    every entry — ~200 `Path.resolve()` syscall chains per request. Held on the loop, that
    stalls not just this call but every request in flight behind it.

    Measured: CI run 30608628129, the first live-fast run in nine to clear the C3 gate, failed
    only here — WK2's `GET /api/watcher` hit its 10 s read timeout while the suite was writing
    the registry from another process and the daemon was re-arming 157 roots after RL2's
    forced reload. Nothing in the endpoint's own work is slow; it was queued behind the loop.

    The cost is head-of-line blocking, not this route's own latency. Under 16 concurrent
    `/api/watcher` calls on an idle box, `/healthz` — which touches none of this — went from a
    median of 301 ms (p95 329) to 82 ms (p95 152). That is with the registry warm in page
    cache and no competing writer; the run that failed had both, and a file lock held across
    processes has no bound at all.
    """
    import asyncio
    body, code = await asyncio.to_thread(_watcher_sync)
    return JSONResponse(body, status_code=code)


def register(app) -> None:
    app.add_route("/", _root, methods=["GET"])
    app.add_route("/api/watcher", _watcher, methods=["GET"])
