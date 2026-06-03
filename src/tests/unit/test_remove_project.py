"""Unit tests for handle_remove_project (manage action="remove_project")."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.asyncio]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmp_registry(tmp_path, monkeypatch):
    import opencode_search.config as cfg
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(cfg, "REGISTRY_PATH", registry_path)
    return tmp_path


def _register(tmp_path, project_path: str):
    """Add a project entry to the test registry."""
    from opencode_search.config import ProjectEntry, load_registry, save_registry
    registry = load_registry()
    registry[project_path] = ProjectEntry(
        path=project_path,
        db_path=str(tmp_path / "indexes" / "idx" / "index"),
    )
    save_registry(registry)


class TestHandleRemoveProject:

    async def test_remove_registered_project(self, tmp_registry):
        """P0: Removing a registered project returns ok and project is gone."""
        from opencode_search.config import load_registry
        from opencode_search.handlers._vacuum import handle_remove_project

        project = str(tmp_registry / "myrepo")
        _register(tmp_registry, project)
        assert project in load_registry()

        result = await handle_remove_project(project)
        assert result["status"] == "ok"
        assert project not in load_registry()

    async def test_remove_nonexistent_project_returns_error(self, tmp_registry):
        """P0: Removing a project not in registry returns status=error."""
        from opencode_search.handlers._vacuum import handle_remove_project
        result = await handle_remove_project(str(tmp_registry / "ghost"))
        assert result["status"] == "error"
        assert "not found" in result["error"].lower()

    async def test_remove_does_not_delete_index_by_default(self, tmp_registry):
        """P0: delete_index=False (default) leaves the index dir untouched."""
        from opencode_search.handlers._vacuum import handle_remove_project

        project = str(tmp_registry / "repo")
        idx_dir = tmp_registry / "indexes" / "idx"
        idx_dir.mkdir(parents=True)
        (idx_dir / "dummy.ldb").write_bytes(b"x" * 100)
        _register(tmp_registry, project)

        result = await handle_remove_project(project, delete_index=False)
        assert result["status"] == "ok"
        assert result["index_deleted"] is False
        assert idx_dir.exists()

    async def test_remove_with_delete_index_removes_dir(self, tmp_registry, monkeypatch):
        """P0: delete_index=True removes the index directory."""
        import opencode_search.config as cfg
        from opencode_search.config import ProjectEntry, save_registry
        from opencode_search.handlers._vacuum import handle_remove_project

        project = str(tmp_registry / "repo")
        idx_dir = tmp_registry / "indexes" / "idx"
        idx_dir.mkdir(parents=True)
        (idx_dir / "data.ldb").write_bytes(b"x" * 1024)

        # Prevent registry migration from changing db_path to the canonical location
        monkeypatch.setattr(cfg, "migrate_project_entry", lambda entry: False)

        registry = cfg.load_registry()
        registry[project] = ProjectEntry(
            path=project,
            db_path=str(idx_dir / "index"),
        )
        save_registry(registry)

        result = await handle_remove_project(project, delete_index=True)
        assert result["status"] == "ok"
        assert result["index_deleted"] is True
        assert not idx_dir.exists()

    async def test_remove_multiple_projects_independently(self, tmp_registry):
        """P0: Multiple projects can be removed one by one."""
        from opencode_search.config import load_registry
        from opencode_search.handlers._vacuum import handle_remove_project

        p1 = str(tmp_registry / "repo1")
        p2 = str(tmp_registry / "repo2")
        _register(tmp_registry, p1)
        _register(tmp_registry, p2)

        await handle_remove_project(p1)
        registry = load_registry()
        assert p1 not in registry
        assert p2 in registry
