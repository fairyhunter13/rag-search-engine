"""Integration tests for federation handlers (no GPU required).

All tests use a tmp registry and tmp directories — no real projects needed.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _make_registry(tmp_path, monkeypatch):
    import opencode_search.config as cfg
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(cfg, "REGISTRY_PATH", registry_path)
    return registry_path


# ---------------------------------------------------------------------------
# handle_discover_federation
# ---------------------------------------------------------------------------

class TestDiscoverFederation:

    def test_empty_dir_returns_zero_discovered(self, tmp_path, monkeypatch):
        """P0: Empty directory has no federation members."""
        from opencode_search.handlers._federation import handle_discover_federation
        root = tmp_path / "root"
        root.mkdir()
        result = _run(handle_discover_federation(str(root)))
        assert result.get("total") == 0
        assert result.get("discovered") == []

    def test_symlink_member_is_discovered(self, tmp_path, monkeypatch):
        """P0: A symlinked subdirectory is discovered as a federation member."""
        from opencode_search.handlers._federation import handle_discover_federation
        root = tmp_path / "root"
        root.mkdir()
        member = tmp_path / "member_repo"
        member.mkdir()
        (root / "member_link").symlink_to(member)

        result = _run(handle_discover_federation(str(root)))
        assert result["total"] >= 1
        assert any(str(member) in m or m == str(member) for m in result["discovered"])

    def test_go_work_member_is_discovered(self, tmp_path, monkeypatch):
        """P0: go.work 'use' directive is parsed and member resolved."""
        from opencode_search.handlers._federation import handle_discover_federation
        root = tmp_path / "root"
        root.mkdir()
        sub = tmp_path / "sub_service"
        sub.mkdir()
        (root / "go.work").write_text(f"go 1.21\nuse (\n    {sub}\n)\n")

        result = _run(handle_discover_federation(str(root)))
        assert str(sub) in result["sources"]["go_work"]

    def test_nested_symlinks_in_subdir_discovered(self, tmp_path, monkeypatch):
        """P0: Symlinks inside a subdirectory (like repositories-ubuntu/) are found."""
        from opencode_search.handlers._federation import handle_discover_federation
        root = tmp_path / "root"
        root.mkdir()
        container = root / "repositories"
        container.mkdir()
        member1 = tmp_path / "repo1"
        member1.mkdir()
        (container / "link1").symlink_to(member1)

        result = _run(handle_discover_federation(str(root)))
        assert str(member1) in result["discovered"]

    def test_nonexistent_path_returns_error(self, tmp_path, monkeypatch):
        """P0: Non-existent path returns an error dict."""
        from opencode_search.handlers._federation import handle_discover_federation
        result = _run(handle_discover_federation(str(tmp_path / "does_not_exist")))
        assert "error" in result


# ---------------------------------------------------------------------------
# handle_list_federation
# ---------------------------------------------------------------------------

class TestListFederation:

    def test_unregistered_project_returns_error(self, tmp_path, monkeypatch):
        """P0: Listing federation on an unregistered project returns an error."""
        _make_registry(tmp_path, monkeypatch)
        from opencode_search.handlers._federation import handle_list_federation
        result = _run(handle_list_federation(str(tmp_path / "not_registered")))
        assert "error" in result

    def test_registered_project_no_members(self, tmp_path, monkeypatch):
        """P0: Registered project with no federation returns empty members list."""
        import opencode_search.config as cfg
        _make_registry(tmp_path, monkeypatch)
        root_dir = tmp_path / "root"
        root_dir.mkdir()
        from opencode_search.config import ProjectEntry, get_project_db_path, save_registry
        registry = {str(root_dir): ProjectEntry(path=str(root_dir), db_path=str(tmp_path / "idx"))}
        save_registry(registry)

        from opencode_search.handlers._federation import handle_list_federation
        result = _run(handle_list_federation(str(root_dir)))
        assert result["total_members"] == 0
        assert result["members"] == []

    def test_registered_project_with_members_shows_them(self, tmp_path, monkeypatch):
        """P0: Project with federation members lists them all."""
        _make_registry(tmp_path, monkeypatch)
        root_dir = tmp_path / "root"
        root_dir.mkdir()
        member_dir = tmp_path / "member"
        member_dir.mkdir()

        from opencode_search.handlers._federation import handle_add_federation_member
        _run(handle_add_federation_member(str(root_dir), str(member_dir)))

        from opencode_search.handlers._federation import handle_list_federation
        result = _run(handle_list_federation(str(root_dir)))
        assert result["total_members"] == 1
        assert any(str(member_dir) in m["path"] or m["path"] == str(member_dir)
                   for m in result["members"])


# ---------------------------------------------------------------------------
# handle_add_federation_member
# ---------------------------------------------------------------------------

class TestAddFederationMember:

    def test_add_member_persists_to_registry(self, tmp_path, monkeypatch):
        """P0: Adding a member saves it to the registry."""
        _make_registry(tmp_path, monkeypatch)
        root_dir = tmp_path / "root"
        root_dir.mkdir()
        member_dir = tmp_path / "member"
        member_dir.mkdir()

        from opencode_search.handlers._federation import handle_add_federation_member
        result = _run(handle_add_federation_member(str(root_dir), str(member_dir)))
        assert result["status"] == "ok"
        assert result["total_members"] == 1

    def test_add_member_twice_is_idempotent(self, tmp_path, monkeypatch):
        """P0: Adding same member twice does not duplicate it."""
        _make_registry(tmp_path, monkeypatch)
        root_dir = tmp_path / "root"
        root_dir.mkdir()
        member_dir = tmp_path / "member"
        member_dir.mkdir()

        from opencode_search.handlers._federation import handle_add_federation_member
        _run(handle_add_federation_member(str(root_dir), str(member_dir)))
        result = _run(handle_add_federation_member(str(root_dir), str(member_dir)))
        assert result["total_members"] == 1

    def test_add_self_as_member_returns_error(self, tmp_path, monkeypatch):
        """P0: Adding the root as its own member returns an error."""
        _make_registry(tmp_path, monkeypatch)
        root_dir = tmp_path / "root"
        root_dir.mkdir()

        from opencode_search.handlers._federation import handle_add_federation_member
        result = _run(handle_add_federation_member(str(root_dir), str(root_dir)))
        assert "error" in result

    def test_add_nonexistent_member_returns_error(self, tmp_path, monkeypatch):
        """P0: Adding a non-existent path as member returns an error."""
        _make_registry(tmp_path, monkeypatch)
        root_dir = tmp_path / "root"
        root_dir.mkdir()

        from opencode_search.handlers._federation import handle_add_federation_member
        result = _run(handle_add_federation_member(str(root_dir), str(tmp_path / "ghost")))
        assert "error" in result


# ---------------------------------------------------------------------------
# handle_remove_federation_member
# ---------------------------------------------------------------------------

class TestRemoveFederationMember:

    def test_remove_member_works(self, tmp_path, monkeypatch):
        """P0: Removing a previously added member succeeds."""
        _make_registry(tmp_path, monkeypatch)
        root_dir = tmp_path / "root"
        root_dir.mkdir()
        member_dir = tmp_path / "member"
        member_dir.mkdir()

        from opencode_search.handlers._federation import (
            handle_add_federation_member,
            handle_remove_federation_member,
        )
        _run(handle_add_federation_member(str(root_dir), str(member_dir)))
        result = _run(handle_remove_federation_member(str(root_dir), str(member_dir)))
        assert result["status"] == "ok"
        assert result["total_members"] == 0

    def test_remove_nonmember_returns_error(self, tmp_path, monkeypatch):
        """P0: Removing a path that is not a member returns an error."""
        _make_registry(tmp_path, monkeypatch)
        root_dir = tmp_path / "root"
        root_dir.mkdir()
        from opencode_search.config import ProjectEntry, save_registry
        save_registry({str(root_dir): ProjectEntry(path=str(root_dir), db_path=str(tmp_path / "i"))})

        from opencode_search.handlers._federation import handle_remove_federation_member
        result = _run(handle_remove_federation_member(str(root_dir), str(tmp_path / "not_member")))
        assert "error" in result

    def test_remove_from_unregistered_returns_error(self, tmp_path, monkeypatch):
        """P0: Removing from an unregistered root returns error."""
        _make_registry(tmp_path, monkeypatch)
        from opencode_search.handlers._federation import handle_remove_federation_member
        result = _run(handle_remove_federation_member(str(tmp_path / "ghost"), str(tmp_path / "m")))
        assert "error" in result
