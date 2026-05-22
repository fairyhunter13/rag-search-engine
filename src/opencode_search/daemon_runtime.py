"""Shared runtime state for the singleton MCP daemon."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _RuntimeState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    active_clients: dict[str, float] = field(default_factory=dict)
    last_activity_monotonic: float = field(default_factory=time.monotonic)

    def note_activity(self) -> None:
        with self.lock:
            self.last_activity_monotonic = time.monotonic()

    def client_open(self, client_id: str) -> None:
        now = time.monotonic()
        with self.lock:
            self.active_clients[client_id] = now
            self.last_activity_monotonic = now

    def client_heartbeat(self, client_id: str) -> None:
        now = time.monotonic()
        with self.lock:
            if client_id in self.active_clients:
                self.active_clients[client_id] = now
            self.last_activity_monotonic = now

    def client_close(self, client_id: str) -> None:
        with self.lock:
            self.active_clients.pop(client_id, None)
            self.last_activity_monotonic = time.monotonic()

    def prune_stale_clients(self, stale_after_s: float) -> int:
        now = time.monotonic()
        with self.lock:
            stale = [
                client_id
                for client_id, heartbeat in self.active_clients.items()
                if now - heartbeat > stale_after_s
            ]
            for client_id in stale:
                self.active_clients.pop(client_id, None)
            return len(stale)

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            now = time.monotonic()
            return {
                "active_clients": len(self.active_clients),
                "client_ids": sorted(self.active_clients.keys()),
                "idle_seconds": round(now - self.last_activity_monotonic, 1),
            }

    def should_shutdown(self, idle_timeout_s: float, stale_after_s: float) -> bool:
        self.prune_stale_clients(stale_after_s)
        with self.lock:
            if self.active_clients:
                return False
            return (time.monotonic() - self.last_activity_monotonic) >= idle_timeout_s


runtime_state = _RuntimeState()

