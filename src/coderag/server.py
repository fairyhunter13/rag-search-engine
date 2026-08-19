"""The daemon: two routes, five threads, and an exit that does not run atexit.

`/healthz` and `/mcp`. The old engine served sixteen routes; journald, a repeat
`index` call and `coderag doctor` answer everything the other fourteen did, and
one of them -- `/api/gpu/release` -- returned 200 without freeing any VRAM at
all, which is worse than not having it.

Three lifecycle facts, each bought with an outage:

**Shutdown calls `os._exit`.** CUDA EP static destructors abort with signal 134
during normal CPython finalization. Under `Restart=on-failure` that is a restart
loop, not a crash -- the unit comes back, exits 134 again, and the only symptom
is a daemon that looks healthy in between.

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
            watch.rearm()
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
    app = mcp.streamable_http_app()
    app.router.lifespan_context = lifespan
    app.add_route("/healthz", healthz, methods=["GET"])
    return app


def serve(host: str = "", port: int = 0) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    uvicorn.run(
        build_app(),
        host=host or config.HOST,
        port=port or config.PORT,
        log_level="info",
        access_log=False,
    )
    _shutdown_exit()


def _shutdown_exit() -> None:
    """Leave without running interpreter finalization.

    Not tidiness -- correctness. The CUDA EP's static destructors abort with 134
    during finalization, and `Restart=on-failure` turns that into a loop.
    """
    log.info("exiting")
    for stream in (1, 2):
        with contextlib.suppress(OSError):
            os.fsync(stream)
    os._exit(0)
