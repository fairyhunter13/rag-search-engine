"""Daemon server: HTTP at :8765, scheduler, watcher, sweeps."""
from __future__ import annotations

import contextlib
import logging
import os
import socket
import threading

log = logging.getLogger(__name__)

_MODEL_IDLE_UNLOAD_S = float(os.environ.get("OPENCODE_MODEL_IDLE_UNLOAD_S", "300"))
_RECONCILE_INITIAL_DELAY_S = float(os.environ.get("OPENCODE_RECONCILE_INITIAL_DELAY_S", "30"))
_RECONCILE_RESYNC_S = float(os.environ.get("OPENCODE_RECONCILE_RESYNC_S", "0"))
_idle_unload_done = False
_reconcile_park = threading.Event()  # never set; parks the reconcile thread when resync is disabled


def _deprioritize_current_thread(delta: int = 5) -> None:
    """Raise the calling thread's niceness so the event loop wins CPU under contention.
    Linux nice() applies to the calling thread (who=0 → current task); non-Linux no-ops."""
    with contextlib.suppress(Exception):
        os.nice(delta)


def _idle_unload() -> None:
    global _idle_unload_done
    from opencode_search.daemon.runtime_state import seconds_since_activity
    idle = seconds_since_activity()
    if idle > _MODEL_IDLE_UNLOAD_S and not _idle_unload_done:
        try:
            import opencode_search.server.mcp as mcp_mod
            mcp_mod._embedder = None
            import opencode_search.query.search as search_mod
            search_mod._reranker = None
            import opencode_search.embed.embedder as _emb_mod
            _emb_mod._default = None
            _idle_unload_done = True
            import ctypes
            import gc
            gc.collect()  # force native ONNX session + CPU thread pool to free now
            with contextlib.suppress(Exception):
                ctypes.CDLL("libc.so.6").malloc_trim(0)  # return freed arena to OS
            log.info("model idle unload after %.0fs idle", idle)
        except Exception as exc:
            log.warning("idle unload failed: %s", exc)
    elif idle < _MODEL_IDLE_UNLOAD_S:
        _idle_unload_done = False


def _sd_notify(msg: str) -> None:
    sock = os.environ.get("NOTIFY_SOCKET", "")
    if not sock:
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s, contextlib.suppress(OSError):
        s.sendto(msg.encode(), sock)

def serve(host: str | None = None, port: int | None = None) -> None:
    """Run uvicorn at host:port; also start scheduler and watcher."""
    import sys

    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s:%(filename)s:%(lineno)d %(message)s",
    )

    from opencode_search.core.config import DAEMON_HOST, DAEMON_PORT
    from opencode_search.core.gpu import assert_gpu_available
    from opencode_search.server.routes import create_app

    assert_gpu_available()  # exit 1 when no GPU EP available; CPU fallback is forbidden
    _start_background()
    app = create_app()
    h = host or DAEMON_HOST
    p = port or DAEMON_PORT
    log.info("serving on http://%s:%d", h, p)
    threading.Thread(target=lambda: (__import__("time").sleep(0.5), _sd_notify("READY=1")), daemon=True).start()
    try:
        uvicorn.run(app, host=h, port=p)
    finally:
        _sd_notify("STOPPING=1")


def start_watcher():
    """Build and start the file watcher for all enabled registered projects."""
    from pathlib import Path

    from opencode_search.core.config import federation_exclude_paths
    from opencode_search.core.registry import list_projects
    from opencode_search.daemon.sweeps import on_change
    from opencode_search.daemon.watcher import Watcher

    _excluded = federation_exclude_paths()
    watcher = Watcher(on_change=on_change)
    watcher.POLL_INTERVAL = 5.0
    for entry in list_projects():
        if entry.enabled and str(Path(entry.path).resolve()) not in _excluded:
            watcher.watch(entry.path)
    watcher.start()
    return watcher


def ensure_running(host: str = "127.0.0.1", port: int = 8765) -> bool:
    """True if the daemon HTTP server is already responding."""
    try:
        import httpx
        r = httpx.get(f"http://{host}:{port}/healthz", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


def _start_background() -> None:
    from opencode_search.daemon.runtime_state import check_idle_shutdown
    from opencode_search.daemon.scheduler import Scheduler
    from opencode_search.daemon.sweeps import maintenance, reconcile_projects

    scheduler = Scheduler()
    scheduler.register("maintenance", maintenance, interval_s=21600.0)  # 6 h; CPU/disk only
    scheduler.register("idle_shutdown", check_idle_shutdown, interval_s=60.0)
    scheduler.register("idle_unload", _idle_unload, interval_s=60.0)
    watchdog_us = int(os.environ.get("WATCHDOG_USEC", "0"))
    if watchdog_us > 0:
        scheduler.register("watchdog", lambda: _sd_notify("WATCHDOG=1"), interval_s=max(1.0, watchdog_us / 2_000_000))
    scheduler.start()

    from opencode_search.daemon.federation import register_all_members
    register_all_members()  # synchronous: members registered before start_watcher() watches them

    start_watcher()

    def _reconcile_loop() -> None:
        import time
        _deprioritize_current_thread()  # de-prioritize immediately; event loop wins CPU post-restart
        time.sleep(_RECONCILE_INITIAL_DELAY_S)  # grace: serve early requests before first sweep
        with contextlib.suppress(Exception):
            reconcile_projects()  # startup-once: heals algo drift + discovers new/partial projects
        # Opt-in periodic resync (default OFF): OPENCODE_RECONCILE_RESYNC_S > 0 enables it.
        # Steady state is watcher-driven (on_change). The thread stays alive so nice+5 is visible.
        if _RECONCILE_RESYNC_S > 0:
            while True:
                time.sleep(_RECONCILE_RESYNC_S)
                with contextlib.suppress(Exception):
                    reconcile_projects()
        else:
            _reconcile_park.wait()  # park with zero CPU; daemon threads die on process exit

    threading.Thread(target=_reconcile_loop, daemon=True, name="reconcile").start()
