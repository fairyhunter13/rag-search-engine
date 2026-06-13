"""Daemon server: HTTP at :8765, scheduler, watcher, sweeps."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_SWEEP_INTERVAL = 1200.0  # 20 min
_WATCH_INTERVAL = 5.0


def serve(host: str | None = None, port: int | None = None) -> None:
    """Run uvicorn at host:port; also start scheduler and watcher."""
    import uvicorn

    from opencode_search.core.config import DAEMON_HOST, DAEMON_PORT
    from opencode_search.server.routes import create_app

    _start_background()
    app = create_app()
    h = host or DAEMON_HOST
    p = port or DAEMON_PORT
    log.info("serving on http://%s:%d", h, p)
    uvicorn.run(app, host=h, port=p)


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
    from opencode_search.daemon.scheduler import Scheduler
    from opencode_search.daemon.sweeps import auto_index, kb_sweep, maintenance
    from opencode_search.daemon.watcher import Watcher

    scheduler = Scheduler()
    scheduler.register("auto_index", auto_index, interval_s=_SWEEP_INTERVAL)
    scheduler.register("kb_sweep", kb_sweep, interval_s=_SWEEP_INTERVAL)
    scheduler.register("maintenance", maintenance, interval_s=_SWEEP_INTERVAL * 3)
    scheduler.start()

    def _on_change(project_path: str, _files: list) -> None:
        from opencode_search.daemon.sweeps import _index_project
        try:
            _index_project(project_path)
        except Exception as exc:
            log.warning("incremental reindex %s: %s", project_path, exc)

    watcher = Watcher(on_change=_on_change)
    watcher.POLL_INTERVAL = _WATCH_INTERVAL
    for entry in list_projects():
        if entry.enabled:
            watcher.watch(entry.path)
    watcher.start()
