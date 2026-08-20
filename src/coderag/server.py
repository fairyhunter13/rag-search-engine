"""The daemon: two routes, five threads, and an exit that does not run atexit.

`/healthz` and `/mcp`. The old engine served sixteen routes; journald, a repeat
`index` call and `coderag doctor` answer everything the other fourteen did, and
one of them -- `/api/gpu/release` -- returned 200 without freeing any VRAM at
all, which is worse than not having it.

Three lifecycle facts, each bought with an outage:

**Shutdown calls `os._exit`.** CUDA EP static destructors abort with signal 134
during normal CPython finalization. Under `Restart=on-failure` that is a restart
loop, not a crash -- the unit comes back, exits 134 again, and the only symptom
is a daemon that looks healthy in between. Reaching that exit needs
`timeout_graceful_shutdown`: uvicorn otherwise waits on MCP's open streams
until systemd SIGKILLs the process and the exit never runs.

**The idle timer is what makes `release_models` real.** Unprompted, an idle
daemon holds 12.2 GB with 3.5 GB free on a 16 GB card, because the ONNX BFC
arena never shrinks. The function existed before; the thing that *called* it did
not.

**The startup reconcile is the only reconcile.** One content-hash pass over
every enabled project catches whatever changed while the daemon was down.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import socket
import threading
from collections.abc import AsyncIterator

import uvicorn
from starlette.responses import JSONResponse

from . import config, embed, index, registry, watch
from .tools import mcp

log = logging.getLogger(__name__)

_ticker: threading.Thread | None = None
_stop = threading.Event()


def _notify(state: str) -> None:
    """sd_notify without the systemd python binding, which is a C extension."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    with contextlib.suppress(OSError), sock:
        sock.connect("\0" + addr[1:] if addr.startswith("@") else addr)
        sock.sendall(state.encode())


def _tick() -> None:
    """One timer for three jobs, because three timers is three things to stop."""
    while not _stop.wait(config.SCHEDULER_TICK_S):
        _notify("WATCHDOG=1")
        with contextlib.suppress(Exception):
            # `start` before the rearm check: a rearm only sets a flag, and a
            # thread that died reads no flags. `start` is a no-op while one is
            # alive. `rearm_if_changed`, never `rearm` -- see the docstring.
            watch.start()
            watch.rearm_if_changed()
        idle = config.MODEL_IDLE_UNLOAD_S
        if idle and embed.loaded() and embed.idle_seconds() > idle:
            log.info("idle for %ds, releasing models", idle)
            embed.release_models()


@contextlib.asynccontextmanager
async def lifespan(_app) -> AsyncIterator[None]:
    index.start_worker()
    queued = index.reconcile_all() if config.RECONCILE_ON_START else 0
    watch.start()

    global _ticker
    _stop.clear()
    _ticker = threading.Thread(target=_tick, name="scheduler", daemon=True)
    _ticker.start()

    log.info("ready: %d projects queued", queued)
    _notify("READY=1")
    try:
        yield
    finally:
        _notify("STOPPING=1")
        # Armed here, in the inner context, so it is running strictly before
        # the session manager's `__aexit__` -- the window that hangs. Its task
        # group waits on children, and a plain-`def` tool runs under anyio's
        # shielded thread, so the cancel cannot reach it and the wait has no
        # bound. Everything below is advisory; this is the one that ends.
        deadline = threading.Timer(config.SHUTDOWN_DEADLINE_S, _shutdown_exit, [True])
        deadline.daemon = True
        deadline.start()
        _stop.set()
        watch.stop()
        index.stop_worker()


async def healthz(_request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "projects": len(registry.enabled_projects()),
            "models_loaded": embed.loaded(),
            "watching": watch.watching(),
            "indexer": index.status(),
        }
    )


def build_app():
    # Stateless: a fresh transport per request, no session id to carry. It is
    # what makes the `2026-07-28` per-request envelope reachable at all -- the
    # stateful path only negotiates up to the last handshake revision -- and
    # this daemon has no subscriptions or sampling for a session to hold.
    app = mcp.streamable_http_app(stateless_http=True)
    served = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def both(scope) -> AsyncIterator[None]:
        # Nested, never replaced. The SDK's lifespan is what enters the session
        # manager's task group, and assigning over it left every `/mcp` call
        # answering 500 "Task group is not initialized" while `/healthz` stayed
        # green -- so the daemon read as up from every check that was not a
        # request.
        async with served(scope), lifespan(scope):
            yield

    app.router.lifespan_context = both
    app.add_route("/healthz", healthz, methods=["GET"])
    return app


def serve(host: str = "", port: int = 0) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # uvicorn restores the handler it replaced and then re-raises the signal it
    # caught, so the exit below never ran on a stop: the process died -15 under
    # the default disposition. Installing this first makes ours the handler it
    # restores, which is what makes `_shutdown_exit` reachable at all.
    signal.signal(signal.SIGTERM, lambda *_: _shutdown_exit())
    uvicorn.run(
        build_app(),
        host=host or config.HOST,
        port=port or config.PORT,
        log_level="info",
        access_log=False,
        # Without this `uvicorn.run` never returns and the exit below is dead
        # code: MCP streamable HTTP holds its connections open, so a graceful
        # shutdown waits for clients that are not going to disconnect. systemd
        # then SIGKILLs at TimeoutStopSec, which fires OnFailure on every
        # deliberate stop -- measured at 90 s and result 'timeout'.
        timeout_graceful_shutdown=5,
    )
    _shutdown_exit()


def _shutdown_exit(on_deadline: bool = False) -> None:
    """Leave without running interpreter finalization.

    Not tidiness -- correctness. The CUDA EP's static destructors abort with 134
    during finalization, and `Restart=on-failure` turns that into a loop.
    """
    # Says so out loud: exit 0 either way, so the journal is the only place the
    # difference between a clean unwind and a forced one is visible.
    log.info("exiting on shutdown deadline" if on_deadline else "exiting")
    for stream in (1, 2):
        with contextlib.suppress(OSError):
            os.fsync(stream)
    os._exit(0)
