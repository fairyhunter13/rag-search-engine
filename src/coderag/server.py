"""The daemon: three routes, five threads, and an exit that does not run atexit.

`/healthz`, `/register` and `/mcp`. The old engine served sixteen routes;
journald, a repeat `index` call and `coderag doctor` answer everything the other
fourteen did, and one of them -- `/api/gpu/release` -- returned 200 without
freeing any VRAM at all, which is worse than not having it. `/register` is the
one route no model calls: a SessionStart hook enrolls the directory it opens in,
and nothing else in the engine can create a row without a model asking for it.

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

**Two reconciles: one at startup, one hourly.** The startup pass catches what
changed while the daemon was down. The hourly sweep re-discovers each root's
members first, because discovery used to run only inside an explicit `index`
call and a symlink added afterwards was never seen.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import threading
import time
from collections.abc import AsyncIterator

import anyio.to_thread
import uvicorn
from starlette.responses import JSONResponse

from . import config, conns, embed, federation, index, registry, runledger, watch, watchdog
from .tools import enroll, mcp

log = logging.getLogger(__name__)

_ticker: threading.Thread | None = None
_stop = threading.Event()
# Read by the shutdown deadline below. `lifespan` also runs in-process under
# TestClient, where the timer bounds no uvicorn and kills the pytest process
# instead -- at exit 0, which reads as a green suite.
_serving = False
# Keyed by job name so a recovery clears only its own entry.
_tick_errors: dict[str, str] = {}


def _sweep() -> None:
    """Re-discover members, then reconcile. Hourly, not per tick.

    Discovery walks every direct root's tree and a reconcile enqueues the whole
    fleet, so this is the expensive half of the timer and it runs on its own
    counter. The re-arm stays conditional: rebuilding ~120,000 inotify watches
    costs 5.4 s over 151 projects and inotify has no replay, so an
    unconditional re-arm here would blind the watcher for seconds every hour.
    """
    started = time.perf_counter()
    claimed, released = federation.sweep()
    if claimed or released:
        log.info("sweep claimed %d, released %d member(s)", len(claimed), len(released))
    for member in claimed:
        index.submit(member, reason="sweep")
    reconciled = index.reconcile_all()
    # A row with no recorded device can never be pruned, so it outlives its own
    # directory and pages hourly. The sweep is where a present path is cheapest
    # to ask about.
    devices = registry.record_devices()
    if devices:
        log.info("recorded a device for %d row(s) that had none", len(devices))
    watch.rearm_if_changed()
    # A sweep that claims nothing logs nothing, and nearly every sweep claims
    # nothing. So the hourly job that enqueues the fleet left no evidence it ran.
    runledger.record(
        "sweep",
        {
            "claimed": [str(m) for m in claimed],
            "released": [str(m) for m in released],
            "reconciled": reconciled,
            "devices": len(devices),
            "took_ms": round((time.perf_counter() - started) * 1000, 2),
        },
    )


def _guarded(name: str, job) -> None:
    """One bad job must not stop the timer -- but suppressing it outright is how
    a sweep that raised every hour stayed invisible: no log line, no registry
    row, and `/healthz` green, because the sweep is what would have recorded it.
    """
    try:
        job()
    except Exception as exc:
        _tick_errors[name] = f"{type(exc).__name__}: {exc}"
        log.exception("scheduler job %s failed", name)
        # `_tick_errors` is memory. A scheduler failure that restarts the daemon
        # is also what erases the only record of itself.
        runledger.record("sched", {"job": name, "error": _tick_errors[name]})
    else:
        _tick_errors.pop(name, None)


def _reap_stores() -> None:
    # Silent when it closes nothing, which is nearly every tick. A reap left no
    # log line and no row, so `/proc/<pid>/fd` was the only evidence it ran, and
    # an audit spent three watches deciding whether it fires at all.
    closed = conns.reap_idle()
    if closed:
        runledger.record("reap", {"closed": closed, "open": conns.open_count()})


def _watch_tick() -> None:
    # `start` before the rearm check: a rearm only sets a flag, and a thread
    # that died reads no flags. `start` is a no-op while one is alive.
    watch.start()
    watch.rearm_if_changed()


def _tick() -> None:
    """One timer for four jobs, because four timers is four things to stop."""
    since_sweep = 0.0
    while not _stop.wait(config.SCHEDULER_TICK_S):
        _guarded("watch", _watch_tick)
        since_sweep += config.SCHEDULER_TICK_S
        if config.SWEEP_EVERY_S and since_sweep >= config.SWEEP_EVERY_S:
            since_sweep = 0.0
            _guarded("sweep", _sweep)
        _guarded("stores", _reap_stores)
        idle = config.MODEL_IDLE_UNLOAD_S
        if idle and embed.loaded() and embed.idle_seconds() > idle:
            log.info("idle for %ds, releasing models", idle)
            embed.release_models()
        watchdog.beat()


@contextlib.asynccontextmanager
async def lifespan(_app) -> AsyncIterator[None]:
    # Each worker keeps its own SQLite handles, so anyio's default 40 is one
    # federated unit's page caches multiplied by 40.
    anyio.to_thread.current_default_thread_limiter().total_tokens = config.THREAD_LIMIT
    index.start_worker()
    queued = index.reconcile_all() if config.RECONCILE_ON_START else 0
    watch.start()

    global _ticker
    _stop.clear()
    _ticker = threading.Thread(target=_tick, name="scheduler", daemon=True)
    _ticker.start()
    watchdog.start(_stop, _ticker)

    log.info("ready: %d projects queued", queued)
    watchdog.notify("READY=1")
    try:
        yield
    finally:
        watchdog.notify("STOPPING=1")
        # Armed here, in the inner context, so it is running strictly before
        # the session manager's `__aexit__` -- the window that hangs. Its task
        # group waits on children, and a plain-`def` tool runs under anyio's
        # shielded thread, so the cancel cannot reach it and the wait has no
        # bound. Everything below is advisory; this is the one that ends.
        if _serving:
            deadline = threading.Timer(config.SHUTDOWN_DEADLINE_S, _shutdown_exit, [True])
            deadline.daemon = True
            deadline.start()
        _stop.set()
        watchdog.disarm()
        watch.stop()
        index.stop_worker()


async def register(request) -> JSONResponse:
    """Enroll a directory whose caller is standing in it, with no model in the loop.

    The `index` tool cannot serve this. It pins the target against the client's
    MCP roots, and a SessionStart hook speaks plain HTTP and carries none. The
    pin is containment rather than authorization on a localhost daemon, which
    `scope` records, so this route widens nothing a `curl` did not already reach.
    """
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "the body is not JSON"}, status_code=400)
    root = (body or {}).get("root")
    if not root:
        return JSONResponse({"error": "the body names no root"}, status_code=400)
    # Off the event loop: enroll walks the tree for members, and `/healthz`
    # answers behind it otherwise.
    return JSONResponse(await anyio.to_thread.run_sync(enroll, registry.resolve(root)))


async def healthz(_request) -> JSONResponse:
    # One load for both: the count and the digest have to describe the same
    # registry, or a fixture comparing them across a run compares two moments.
    rows = registry.load()
    failing = sorted(k for k, e in rows.items() if e.enabled and e.last_error)
    return JSONResponse(
        {
            "status": "ok",
            "projects": sum(1 for e in rows.values() if e.enabled),
            # A liveness check answers "is the process up", which stayed green through
            # every project failing to index. These two are what make that visible.
            "projects_failing": len(failing),
            # Identities, not just the count: a checker deciding "still failing" has to
            # compare the same projects across two runs, and a count cannot tell one
            # project failing twice from two failing once each.
            "failing": failing[: config.HEALTH_FAILING_CAP],
            # A dead scheduler is the failure no per-project field can carry: the
            # sweep is what would have written one.
            # The watcher joins them under its own key: it runs on its own thread,
            # so `_guarded` never sees its failures, and a caught `OSError` leaves
            # a live thread watching nothing.
            "scheduler_errors": dict(_tick_errors)
            | ({"watch": watch.error()} if watch.error() else {})
            # The withheld ping is a restart in 90 s, so it has to be readable
            # before the restart erases the journal context that explains it.
            | ({"heartbeat": stalled} if (stalled := watchdog.stall()) else {}),
            "errors_total": sum(e.error_total for e in rows.values()),
            "fleet_digest": registry.fleet_digest(rows),
            "unclaimed_stores": len(registry.unclaimed_stores()),
            "models_loaded": embed.loaded(),
            "providers": embed.bound_providers(),
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
    app.add_route("/register", register, methods=["POST"])
    return app


def serve(host: str = "", port: int = 0) -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # `Terminating session: None` once per /mcp request -- 11,649 lines a day,
    # burying the hourly `sweep claimed N`, which is this daemon's only
    # production signal. Not a bug: `stateless_http=True` means there is no
    # session id to name. A level rather than a filter, because a filter keyed
    # on the message breaks in silence at the next SDK release. Here and not in
    # `build_app`, which the tests import.
    logging.getLogger("mcp.server.streamable_http").setLevel(logging.WARNING)
    if config.LOG_LEVEL != "DEBUG":
        # 3,800 of one day's 5,912 journal lines, each announcing a change count
        # and naming no project. The watch ledger row names one.
        logging.getLogger("watchfiles").setLevel(logging.WARNING)
    global _serving
    _serving = True
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
