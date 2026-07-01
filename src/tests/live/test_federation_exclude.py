"""FE — OPENCODE_FEDERATION_EXCLUDE integration tests.

FE1 — federation_exclude_paths() is empty by default.
FE2 — discover_members() includes a symlinked external repo when not excluded.
FE3 — discover_members() skips a symlinked external repo when its resolved path is excluded.
FE4 — federation_exclude_paths() handles ~ expansion, multiple paths, and blank entries.
FE5 — is_federation_excluded() excludes by prefix (subtree, not just exact path).
FE6 — discover_members() skips a member when matched by a glob entry.
FE7 — is_federation_excluded() unit-table: empty / exact / child-of-prefix / glob / non-match.
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


def test_fe5_prefix_dir_excludes_subtree(tmp_path):
    """FE5: is_federation_excluded() returns True for a child of an excluded prefix dir."""
    from opencode_search.core.config import is_federation_excluded

    parent = tmp_path / "excluded-parent"
    parent.mkdir()
    child = parent / "nested-child"
    child.mkdir()

    orig = os.environ.get("OPENCODE_FEDERATION_EXCLUDE")
    os.environ["OPENCODE_FEDERATION_EXCLUDE"] = str(parent)
    try:
        assert is_federation_excluded(str(child)), "child of excluded prefix dir must be excluded"
        assert not is_federation_excluded(str(tmp_path / "other-dir")), (
            "unrelated path must not be excluded"
        )
    finally:
        if orig is None:
            del os.environ["OPENCODE_FEDERATION_EXCLUDE"]
        else:
            os.environ["OPENCODE_FEDERATION_EXCLUDE"] = orig


def test_fe6_glob_entry_excludes_member(tmp_path):
    """FE6: discover_members() skips a member matched by a glob in OPENCODE_FEDERATION_EXCLUDE."""
    from opencode_search.daemon.federation import discover_members

    root = tmp_path / "fed-root"
    root.mkdir()
    ext = tmp_path / "vendor-cache-svc"
    ext.mkdir()
    (ext / "main.go").write_text("package main\n")
    (root / "vendor-cache-svc").symlink_to(ext)
    ext_resolved = str(ext.resolve())

    orig = os.environ.get("OPENCODE_FEDERATION_EXCLUDE")
    os.environ["OPENCODE_FEDERATION_EXCLUDE"] = "*/vendor-cache-*"
    try:
        members = discover_members(str(root))
    finally:
        if orig is None:
            del os.environ["OPENCODE_FEDERATION_EXCLUDE"]
        else:
            os.environ["OPENCODE_FEDERATION_EXCLUDE"] = orig

    assert ext_resolved not in members, (
        f"glob-excluded vendor-cache-svc must not appear in members; got {members}"
    )


def test_fe7_is_federation_excluded_unit_table(tmp_path):
    """FE7: is_federation_excluded() truth table: empty/exact/prefix/glob/non-match."""
    from opencode_search.core.config import is_federation_excluded

    exact_dir = (tmp_path / "exact-svc").resolve()
    exact_dir.mkdir()
    child_dir = exact_dir / "sub"
    child_dir.mkdir()
    other_dir = (tmp_path / "other-svc").resolve()
    other_dir.mkdir()

    orig = os.environ.get("OPENCODE_FEDERATION_EXCLUDE")
    os.environ["OPENCODE_FEDERATION_EXCLUDE"] = os.pathsep.join([
        str(exact_dir), "*/glob-svc/*", "",
    ])
    try:
        assert not is_federation_excluded(""), "empty string → False"
        assert is_federation_excluded(str(exact_dir)), "exact match → True"
        assert is_federation_excluded(str(child_dir)), "child of prefix → True"
        assert is_federation_excluded("/nonexistent/glob-svc/anything"), "glob match → True"
        assert not is_federation_excluded(str(other_dir)), "non-matching → False"
    finally:
        if orig is None:
            del os.environ["OPENCODE_FEDERATION_EXCLUDE"]
        else:
            os.environ["OPENCODE_FEDERATION_EXCLUDE"] = orig
