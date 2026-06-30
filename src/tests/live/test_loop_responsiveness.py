"""LOOP — daemon event loop stays responsive while heavy calls are in flight.

LOOP1 — /healthz responds < 2s while /api/graph_export is in flight (single heavy call).
LOOP2 — /healthz responds < 2s while /api/overview(status) + /api/graph_export are both
         in flight (two concurrent heavy calls; faithfully reproduces the post-restart block).
"""
from __future__ import annotations

import concurrent.futures
import time

import pytest
import requests

pytestmark = pytest.mark.live

_DAEMON = "http://127.0.0.1:8765"
_HEALTHZ_SLA_S = 2.0


def test_loop1_healthz_during_graph_export(live_client, federation_root_path):
    """LOOP1: /healthz must respond < 2s while /api/graph_export is in flight."""
    results: list[tuple] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        heavy = pool.submit(
            requests.get,
            f"{_DAEMON}/api/graph_export",
            params={"project": federation_root_path, "max_nodes": "5000"},
            timeout=120,
        )
        time.sleep(0.15)
        for _ in range(4):
            t0 = time.monotonic()
            r = requests.get(f"{_DAEMON}/healthz", timeout=_HEALTHZ_SLA_S + 1)
            elapsed = time.monotonic() - t0
            results.append((r.json().get("ok"), elapsed))
            time.sleep(0.25)
        heavy.result(timeout=120)

    for ok, elapsed in results:
        assert ok is True, f"/healthz ok != True: {ok}"
        assert elapsed < _HEALTHZ_SLA_S, (
            f"/healthz took {elapsed:.2f}s > {_HEALTHZ_SLA_S}s while graph_export in flight "
            f"— event loop may be wedged (offload missing?)"
        )


def test_loop2_healthz_during_overview_and_graph(live_client, federation_root_path):
    """LOOP2: /healthz must respond < 2s while overview(status)+graph_export are both in flight."""
    results: list[tuple] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        f_overview = pool.submit(
            requests.post,
            f"{_DAEMON}/api/overview",
            json={"what": "status", "project": federation_root_path},
            timeout=120,
        )
        f_graph = pool.submit(
            requests.get,
            f"{_DAEMON}/api/graph_export",
            params={"project": federation_root_path, "max_nodes": "5000"},
            timeout=120,
        )
        time.sleep(0.2)
        for _ in range(5):
            t0 = time.monotonic()
            r = requests.get(f"{_DAEMON}/healthz", timeout=_HEALTHZ_SLA_S + 1)
            elapsed = time.monotonic() - t0
            results.append((r.json().get("ok"), elapsed))
            time.sleep(0.3)
        t_overview_start = time.monotonic()
        f_overview.result(timeout=120)
        overview_elapsed = time.monotonic() - t_overview_start
        f_graph.result(timeout=120)

    for ok, elapsed in results:
        assert ok is True, f"/healthz ok != True: {ok}"
        assert elapsed < _HEALTHZ_SLA_S, (
            f"/healthz took {elapsed:.2f}s > {_HEALTHZ_SLA_S}s while overview+graph in flight "
            f"— event loop may be wedged (offload missing?)"
        )
    # Loose upper-bound: overview(status) must not regress to minutes.
    assert overview_elapsed < 120.0, (
        f"overview(status) took {overview_elapsed:.1f}s > 120s — status cost reduction failed"
    )
