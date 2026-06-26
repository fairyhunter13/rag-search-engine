"""Project path resolvers for the live test suite.

Resolvers prefer sample workspace paths (built by the `sample_workspace` session
fixture in conftest.py). Fall back to registry capability discovery when the sample
workspace is absent. Hard-fail (never skip) when nothing matches.

Prefer using the `federation_root_path`, `standalone_project_path`, and
`service_member_path` fixtures in conftest.py rather than calling these directly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_SAFE_BASE = Path.home() / ".local" / "share" / "ocs-test-dirs"


def _sample_ws() -> Path | None:
    """Most-recently created sample-ws- dir, or None if absent."""
    candidates = sorted(_SAFE_BASE.glob("sample-ws-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _enabled_projects():
    from opencode_search.core.registry import list_projects
    return [p for p in list_projects() if p.enabled]


def federation_root() -> str:
    """shop-federation root from the sample workspace, else first registry federation root."""
    ws = _sample_ws()
    if ws:
        p = ws / "shop-federation"
        if p.is_dir():
            return str(p)
    hit = next((p.path for p in _enabled_projects() if getattr(p, "federation", None)), None)
    if hit:
        return hit
    pytest.fail(
        "No federation root found — start the sample_workspace fixture before this test."
    )


def standalone_project() -> str:
    """ledger-standalone from the sample workspace, else first registry standalone project."""
    ws = _sample_ws()
    if ws:
        p = ws / "ledger-standalone"
        if p.is_dir():
            return str(p)
    from opencode_search.core.config import project_vector_db
    hit = next(
        (p.path for p in _enabled_projects()
         if not getattr(p, "federation", None) and project_vector_db(p.path).exists()),
        None,
    )
    if hit:
        return hit
    pytest.fail(
        "No standalone project found — start the sample_workspace fixture before this test."
    )


def service_member() -> str:
    """promo-svc from the sample workspace (business-rule-rich), else first registry member."""
    ws = _sample_ws()
    if ws:
        p = ws / "shop-federation" / "promo-svc"
        if p.is_dir():
            return str(p)
    root = federation_root()
    from opencode_search.core.registry import list_projects
    root_entry = next((p for p in list_projects() if p.path == root), None)
    if root_entry and getattr(root_entry, "federation", None):
        enabled = {p.path for p in list_projects() if p.enabled}
        hit = next((m for m in root_entry.federation if m in enabled), None)
        if hit:
            return hit
    pytest.fail(
        "No federation member found — start the sample_workspace fixture before this test."
    )
