"""Daemon server: HTTP at :8765, scheduler, watcher, sweeps."""
from __future__ import annotations

import contextlib
import logging
import os
import socket
import threading

log = logging.getLogger(__name__)

_MODEL_IDLE_UNLOAD_S = float(os.environ.get("OPENCODE_MODEL_IDLE_UNLOAD_S", "300"))
_idle_unload_done = False


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
    import uvicorn

    from opencode_search.core.config import DAEMON_HOST, DAEMON_PORT
    from opencode_search.core.gpu import assert_cuda_available
    from opencode_search.server.routes import create_app

    assert_cuda_available()  # exit 1 on no-CUDA; CPU fallback is forbidden
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
    from opencode_search.core.registry import list_projects
    from opencode_search.daemon.sweeps import on_change
    from opencode_search.daemon.watcher import Watcher

    watcher = Watcher(on_change=on_change)
    watcher.POLL_INTERVAL = 5.0
    for entry in list_projects():
        if entry.enabled:
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
    from opencode_search.core.registry import list_projects
    from opencode_search.daemon.runtime_state import check_idle_shutdown, release_stale_clients
    from opencode_search.daemon.scheduler import Scheduler
    from opencode_search.daemon.sweeps import _index_project, maintenance

    scheduler = Scheduler()
    scheduler.register("maintenance", maintenance, interval_s=21600.0)  # 6 h; CPU/disk only
    scheduler.register("idle_shutdown", check_idle_shutdown, interval_s=60.0)
    scheduler.register("stale_clients", release_stale_clients, interval_s=60.0)
    scheduler.register("idle_unload", _idle_unload, interval_s=60.0)
    watchdog_us = int(os.environ.get("WATCHDOG_USEC", "0"))
    if watchdog_us > 0:
        scheduler.register("watchdog", lambda: _sd_notify("WATCHDOG=1"), interval_s=max(1.0, watchdog_us / 2_000_000))
    scheduler.start()

    # Resume stalled pipelines: projects with files but no communities.
    for entry in list_projects():
        if entry.enabled and entry.file_count > 0:
            from opencode_search.core.config import project_graph_db
            from opencode_search.graph.store import GraphStore
            gdb = project_graph_db(entry.path)
            if gdb.exists():
                gs = GraphStore(gdb)
                try:
                    if gs.community_count() == 0:
                        log.info("resume stalled pipeline: %s", entry.path)
                        _index_project(entry.path)
                finally:
                    gs.close()

    start_watcher()
