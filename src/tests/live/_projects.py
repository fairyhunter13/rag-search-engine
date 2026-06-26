"""Capability-based project discovery for the live test suite.

Resolvers return paths to the sample workspace fixture (built by the session-scoped
conftest fixture). Falls back to registry capability discovery until the sample workspace
wiring is complete. Hard-fails (never skips) when nothing matches — preserving the
no-skip invariant.
"""
from __future__ import annotations

import pytest


def _enabled_projects():
    from opencode_search.core.registry import list_projects
    return [p for p in list_projects() if p.enabled]


def federation_root() -> str:
    """First enabled project that has federation members. Hard-fails when nothing matches."""
    hit = next(
        (p.path for p in _enabled_projects() if getattr(p, "federation", None)),
        None,
    )
    if hit:
        return hit
    pytest.fail(
        "No enabled project with federation members found. "
        "Register a federation root (e.g. run the sample-workspace fixture)."
    )


def standalone_project() -> str:
    """First enabled, indexed, non-federation project. Hard-fails when nothing matches."""
    from opencode_search.core.config import project_vector_db

    hit = next(
        (p.path for p in _enabled_projects()
         if not getattr(p, "federation", None) and project_vector_db(p.path).exists()),
        None,
    )
    if hit:
        return hit
    pytest.fail(
        "No enabled standalone indexed project found. "
        "Register a standalone project (e.g. run the sample-workspace fixture)."
    )


def service_member() -> str:
    """First enabled member of the federation root. Hard-fails when nothing matches."""
    root = federation_root()
    from opencode_search.core.registry import list_projects
    root_entry = next((p for p in list_projects() if p.path == root), None)
    if root_entry and getattr(root_entry, "federation", None):
        members = root_entry.federation
        enabled_paths = {p.path for p in list_projects() if p.enabled}
        hit = next((m for m in members if m in enabled_paths), None)
        if hit:
            return hit
    pytest.fail(
        "No enabled federation member found. "
        "Register a federation with members (e.g. run the sample-workspace fixture)."
    )
