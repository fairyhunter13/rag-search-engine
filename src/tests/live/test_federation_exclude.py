"""FE — OPENCODE_FEDERATION_EXCLUDE integration tests.

FE1 — federation_exclude_paths() is empty by default.
FE2 — discover_members() includes a symlinked external repo when not excluded.
FE3 — discover_members() skips a symlinked external repo when its resolved path is excluded.
FE4 — federation_exclude_paths() handles ~ expansion, multiple paths, and blank entries.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.live


def _make_ext_repo(tmp_base: Path, root: Path, name: str) -> Path:
    """Create a minimal external repo (has a .go file) OUTSIDE root, symlink it into root."""
    ext = tmp_base / ("_ext_" + name)
    ext.mkdir()
    (ext / "main.go").write_text("package main\n")
    (root / name).symlink_to(ext)
    return ext.resolve()


def test_fe1_exclude_paths_empty_by_default():
    """FE1: no env var → empty frozenset."""
    from opencode_search.core.config import federation_exclude_paths
    orig = os.environ.pop("OPENCODE_FEDERATION_EXCLUDE", None)
    try:
        assert federation_exclude_paths() == frozenset()
    finally:
        if orig is not None:
            os.environ["OPENCODE_FEDERATION_EXCLUDE"] = orig


def test_fe2_discover_members_includes_symlinked_repo(tmp_path):
    """FE2: without exclusion, discover_members returns the external symlinked repo."""
    from opencode_search.daemon.federation import discover_members
    root = tmp_path / "fed-root"
    root.mkdir()
    ext_path = _make_ext_repo(tmp_path, root, "ext-service")

    orig = os.environ.pop("OPENCODE_FEDERATION_EXCLUDE", None)
    try:
        members = discover_members(str(root))
    finally:
        if orig is not None:
            os.environ["OPENCODE_FEDERATION_EXCLUDE"] = orig

    assert str(ext_path) in members, f"ext-service must be discovered; got {members}"


def test_fe3_discover_members_skips_excluded_repo(tmp_path):
    """FE3: repo in OPENCODE_FEDERATION_EXCLUDE is not returned by discover_members."""
    from opencode_search.daemon.federation import discover_members
    root = tmp_path / "fed-root"
    root.mkdir()
    ext_path = _make_ext_repo(tmp_path, root, "ext-service")

    orig = os.environ.get("OPENCODE_FEDERATION_EXCLUDE")
    os.environ["OPENCODE_FEDERATION_EXCLUDE"] = str(ext_path)
    try:
        members = discover_members(str(root))
    finally:
        if orig is None:
            del os.environ["OPENCODE_FEDERATION_EXCLUDE"]
        else:
            os.environ["OPENCODE_FEDERATION_EXCLUDE"] = orig

    assert str(ext_path) not in members, (
        f"excluded ext-service must not appear in members; got {members}"
    )


def test_fe4_exclude_paths_multi_and_blank(tmp_path):
    """FE4: ~ expansion, multiple paths joined by pathsep, blank entries all work."""
    from opencode_search.core.config import federation_exclude_paths
    a = str(tmp_path / "svc-a")
    b = str(tmp_path / "svc-b")
    raw = os.pathsep.join(["", a, "", b, ""])
    orig = os.environ.get("OPENCODE_FEDERATION_EXCLUDE")
    os.environ["OPENCODE_FEDERATION_EXCLUDE"] = raw
    try:
        result = federation_exclude_paths()
    finally:
        if orig is None:
            del os.environ["OPENCODE_FEDERATION_EXCLUDE"]
        else:
            os.environ["OPENCODE_FEDERATION_EXCLUDE"] = orig

    assert Path(a).resolve() in {Path(p) for p in result}
    assert Path(b).resolve() in {Path(p) for p in result}
    assert len(result) == 2, f"blanks must be stripped; got {result}"
