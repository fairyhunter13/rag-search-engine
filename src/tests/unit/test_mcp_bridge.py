"""Unit tests for mcp_bridge workspace restriction logic."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from opencode_search.mcp_bridge import _ensure_within_workspace


def _with_workspace(root: Path, path: str, what: str = "search") -> dict | None:
    """Helper: run _ensure_within_workspace with a fixed root."""
    with patch("opencode_search.mcp_bridge._get_workspace_root", return_value=root):
        with patch("opencode_search.mcp_bridge._allow_outside_workspace", return_value=False):
            return _ensure_within_workspace(path, what=what)


class TestEnsureWithinWorkspace:
    def test_path_directly_inside_workspace_allowed(self, tmp_path):
        sub = tmp_path / "src" / "main.py"
        sub.parent.mkdir(parents=True)
        sub.touch()
        assert _with_workspace(tmp_path, str(sub)) is None

    def test_path_outside_workspace_blocked(self, tmp_path):
        other = tmp_path.parent / "other_project" / "file.py"
        result = _with_workspace(tmp_path, str(other))
        assert result is not None
        assert "restricted to the currently opened workspace" in result["error"]

    def test_symlink_inside_workspace_allowed(self, tmp_path):
        """Symlink under workspace dir is allowed even if its target is outside."""
        real_target = tmp_path.parent / "external_repo"
        real_target.mkdir()
        (real_target / "main.go").touch()

        symlink = tmp_path / "repositories" / "ext-repo"
        symlink.parent.mkdir()
        symlink.symlink_to(real_target)

        # User passes the symlink path — should be allowed
        assert _with_workspace(tmp_path, str(symlink / "main.go")) is None

    def test_symlink_outside_workspace_blocked(self, tmp_path):
        """A path not under workspace (no symlink route in) is blocked."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "completely_outside" / "file.py"
        result = _with_workspace(workspace, str(outside))
        assert result is not None

    def test_allow_outside_workspace_env_bypasses_check(self, tmp_path):
        """OPENCODE_ALLOW_INDEX_OUTSIDE_CWD=1 skips all checks."""
        other = tmp_path.parent / "other_project" / "file.py"
        with patch("opencode_search.mcp_bridge._allow_outside_workspace", return_value=True):
            assert _ensure_within_workspace(str(other), what="search") is None

    def test_workspace_root_itself_allowed(self, tmp_path):
        assert _with_workspace(tmp_path, str(tmp_path)) is None

    def test_error_contains_workspace_root(self, tmp_path):
        other = tmp_path.parent / "other" / "file.py"
        result = _with_workspace(tmp_path, str(other))
        assert str(tmp_path) in result["error"]
