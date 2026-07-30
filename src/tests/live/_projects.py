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

# These resolvers only *find* a workspace that some fixture already built, so calling one directly
# makes a test pass in a full run and fail when its file is run alone — which is how it reads as a
# capability failure. The message names the fixture because the fix is to request it, not to run
# more files first.
_ABSENT = (
    "No sample {what} found. This test calls the resolver directly, so it depends on another "
    "file's `sample_workspace` fixture having run first. Take the `{fixture}` fixture as a test "
    "argument instead — then it works standalone."
)


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
    pytest.fail(_ABSENT.format(what="federation root", fixture="federation_root_path"))


def standalone_project() -> str:
    """ledger-standalone from the sample workspace."""
    ws = _sample_ws()
    if ws:
        p = ws / "ledger-standalone"
        if p.is_dir():
            return str(p)
    pytest.fail(_ABSENT.format(what="standalone project", fixture="standalone_project_path"))


def service_member() -> str:
    """promo-svc (business-rule-rich) from the sample workspace."""
    ws = _sample_ws()
    if ws:
        p = ws / "shop-federation" / "promo-svc"
        if p.is_dir():
            return str(p)
    pytest.fail(_ABSENT.format(what="service member", fixture="service_member_path"))


def sample_project_paths(ws) -> set[str]:
    """All sample workspace paths (root + members + ledger). Used for fleet-wide scope guards."""
    return {ws.fed_root, ws.cart, ws.checkout, ws.promo, ws.ledger}
