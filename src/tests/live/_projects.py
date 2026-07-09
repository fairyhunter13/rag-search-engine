"""Project path resolvers for the live test suite.

Returns sample workspace paths (built by the `sample_workspace` session fixture
in conftest.py). Hard-fails with a clear message if the workspace is absent.
Never falls back to registry projects — a test must never silently bind to a
real device project.

Prefer using the `federation_root_path`, `standalone_project_path`, and
`service_member_path` fixtures in conftest.py rather than calling these directly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_SAFE_BASE = Path.home() / ".local" / "share" / "rse-test-dirs"


def _sample_ws() -> Path | None:
    """Most-recently created sample-ws- dir, or None if absent."""
    candidates = sorted(_SAFE_BASE.glob("sample-ws-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def federation_root() -> str:
    """shop-federation root from the sample workspace."""
    ws = _sample_ws()
    if ws:
        p = ws / "shop-federation"
        if p.is_dir():
            return str(p)
    pytest.fail(
        "No sample federation root found — the sample_workspace fixture must run before this test."
    )


def standalone_project() -> str:
    """ledger-standalone from the sample workspace."""
    ws = _sample_ws()
    if ws:
        p = ws / "ledger-standalone"
        if p.is_dir():
            return str(p)
    pytest.fail(
        "No sample standalone project found — the sample_workspace fixture must run before this test."
    )


def service_member() -> str:
    """promo-svc (business-rule-rich) from the sample workspace."""
    ws = _sample_ws()
    if ws:
        p = ws / "shop-federation" / "promo-svc"
        if p.is_dir():
            return str(p)
    pytest.fail(
        "No sample service member found — the sample_workspace fixture must run before this test."
    )


def sample_project_paths(ws) -> set[str]:
    """All sample workspace paths (root + members + ledger). Used for fleet-wide scope guards."""
    return {ws.fed_root, ws.cart, ws.checkout, ws.promo, ws.ledger}
