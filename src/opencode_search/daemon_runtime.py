"""Shared runtime state for the singleton MCP daemon."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class _RuntimeState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    active_clients: dict[str, float] = field(default_factory=dict)
    client_cwds: dict[str, str] = field(default_factory=dict)
    client_projects: dict[str, str] = field(default_factory=dict)
    closing_clients: set[str] = field(default_factory=set)
    last_activity_monotonic: float = field(default_factory=time.monotonic)
    # Tracks the last time a real interactive query (ask/search/graph) was dispatched.
    # Updated only by explicit tool calls — NOT by 15s heartbeats — so background
    # build loops can distinguish "user is actively querying" from "client is idle".
    last_query_monotonic: float = field(default_factory=lambda: 0.0)

    def note_activity(self) -> None:
        with self.lock:
            self.last_activity_monotonic = time.monotonic()

    def note_query(self) -> None:
        """Called when a real interactive query (ask/search/graph) is dispatched."""
        with self.lock:
            now = time.monotonic()
            self.last_query_monotonic = now
            self.last_activity_monotonic = now

    def seconds_since_last_query(self) -> float:
        with self.lock:
            if self.last_query_monotonic == 0.0:
                return float("inf")
            return time.monotonic() - self.last_query_monotonic

    def client_open(self, client_id: str, cwd: str | None = None, project_path: str | None = None) -> None:
        now = time.monotonic()
        with self.lock:
            self.active_clients[client_id] = now
            if cwd:
                self.client_cwds[client_id] = cwd
            if project_path:
                self.client_projects[client_id] = project_path
            self.closing_clients.discard(client_id)
            self.last_activity_monotonic = now

    def client_heartbeat(self, client_id: str) -> None:
        now = time.monotonic()
        with self.lock:
            if client_id in self.active_clients:
                self.active_clients[client_id] = now
                self.closing_clients.discard(client_id)
            self.last_activity_monotonic = now

    def client_close(self, client_id: str) -> str | None:
        with self.lock:
            now = time.monotonic()
            project_path = self.client_projects.get(client_id)
            if client_id in self.active_clients:
                self.active_clients[client_id] = now
                self.closing_clients.add(client_id)
            self.last_activity_monotonic = now
            return project_path

    def project_client_count(self, project_path: str) -> int:
        with self.lock:
            return sum(1 for value in self.client_projects.values() if value == project_path)

    def _prune_stale_clients_locked(self, stale_after_s: float) -> tuple[int, list[str]]:
        now = time.monotonic()
        stale = [
            client_id
            for client_id, heartbeat in self.active_clients.items()
            if now - heartbeat > stale_after_s
        ]
        candidate_projects: set[str] = set()
        for client_id in stale:
            self.active_clients.pop(client_id, None)
            self.closing_clients.discard(client_id)
            self.client_cwds.pop(client_id, None)
            project_path = self.client_projects.pop(client_id, None)
            if project_path:
                candidate_projects.add(project_path)
        released_projects = [
            project_path
            for project_path in candidate_projects
            if project_path not in self.client_projects.values()
        ]
        return len(stale), released_projects

    def prune_stale_clients(self, stale_after_s: float) -> int:
        with self.lock:
            stale_count, _ = self._prune_stale_clients_locked(stale_after_s)
            return stale_count

    def releaseable_stale_projects(self, stale_after_s: float) -> list[str]:
        with self.lock:
            _, released_projects = self._prune_stale_clients_locked(stale_after_s)
            return released_projects

    def bind_clients_to_project(self, project_path: str) -> int:
        project_root = Path(project_path)
        bound = 0
        with self.lock:
            for client_id, cwd in self.client_cwds.items():
                if client_id in self.closing_clients:
                    continue
                try:
                    cwd_path = Path(cwd)
                    if not cwd_path.is_relative_to(project_root):
                        continue
                    current_project = self.client_projects.get(client_id)
                    if current_project:
                        current_root = Path(current_project)
                        if current_root == project_root:
                            bound += 1
                            continue
                        if current_root.is_relative_to(project_root):
                            # Keep the more specific child-project binding.
                            continue
                    self.client_projects[client_id] = project_path
                    bound += 1
                except Exception:
                    continue
            if bound:
                self.last_activity_monotonic = time.monotonic()
        return bound

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            now = time.monotonic()
            return {
                "active_clients": len(self.active_clients),
                "client_ids": sorted(self.active_clients.keys()),
                "active_projects": sorted(set(self.client_projects.values())),
                "closing_clients": sorted(self.closing_clients),
                "idle_seconds": round(now - self.last_activity_monotonic, 1),
            }

    def should_shutdown(self, idle_timeout_s: float, stale_after_s: float) -> bool:
        self.prune_stale_clients(stale_after_s)
        with self.lock:
            if self.active_clients:
                return False
            return (time.monotonic() - self.last_activity_monotonic) >= idle_timeout_s


runtime_state = _RuntimeState()

# Set by _broadcast_reload_notice() before SIGTERM so open SSE generators can
# emit a {"type":"reload"} frame and close cleanly instead of being severed.
reload_pending = threading.Event()


async def yield_while_busy(
    grace_s: float | None = None,
    max_wait_s: float = 300.0,
    check_interval_s: float = 5.0,
) -> None:
    """Pause a background build loop while an interactive query is active or GPU is hot.

    Call this at each batch boundary inside enrichment/pipeline loops so that
    interactive ask/search/graph calls always get the full GPU with no eviction
    of qwen3-query:8b. The build resumes automatically once the query finishes
    and the GPU cools below the thermal threshold.

    grace_s: seconds after a query to keep yielding (default: OPENCODE_BUILD_QUERY_GRACE_S or 30)
    max_wait_s: safety cap — never block longer than this (default 300 s)
    """
    import asyncio
    import os

    if grace_s is None:
        grace_s = float(os.environ.get("OPENCODE_BUILD_QUERY_GRACE_S", "30"))

    waited = 0.0
    while waited < max_wait_s:
        age = runtime_state.seconds_since_last_query()
        if age < grace_s:
            await asyncio.sleep(check_interval_s)
            waited += check_interval_s
            continue
        try:
            from opencode_search.embeddings import _get_gpu_temp_c
            from opencode_search.indexer import _MAX_GPU_TEMP
            temp = _get_gpu_temp_c()
            if temp is not None and temp > _MAX_GPU_TEMP:
                await asyncio.sleep(check_interval_s)
                waited += check_interval_s
                continue
        except Exception:
            pass
        break
