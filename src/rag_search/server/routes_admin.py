"""Root redirect, and the watcher's own view of what it is watching."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse


async def _root(request: Request) -> RedirectResponse:
    return RedirectResponse("/dashboard")


async def _watcher(request: Request) -> JSONResponse:
    """The armed root set, the pending queue, and what the registry says it should be.

    Both sides are returned because the interesting failure is the *difference*: roots armed
    for projects since disabled, and enabled projects never armed. Answering that used to
    require a py-spy dump of the reader thread's locals.
    """
    from rag_search.daemon.server import get_watcher, watch_roots

    watcher = get_watcher()
    if watcher is None:
        return JSONResponse({"running": False}, status_code=503)
    status = watcher.status()
    expected = sorted(watch_roots())
    armed = set(status.get("roots") or [])
    status["running"] = True
    status["expected"] = expected
    status["missing"] = sorted(set(expected) - armed)   # enabled, registered, never armed
    status["extra"] = sorted(armed - set(expected))     # armed for something now disabled
    return JSONResponse(status)


def register(app) -> None:
    app.add_route("/", _root, methods=["GET"])
    app.add_route("/api/watcher", _watcher, methods=["GET"])
