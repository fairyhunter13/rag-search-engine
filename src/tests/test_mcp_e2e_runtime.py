"""Real MCP tool end-to-end tests against the installed runtime stack."""
# ruff: noqa: E402

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("lancedb")
pytest.importorskip("pyarrow")
pytest.importorskip("mcp")

import opencode_search.mcp as mcp_mod
from opencode_search import config
from opencode_search.mcp import (
    client_close,
    client_open,
    index_project,
    list_indexed_projects,
    project_status,
    resume_watchers,
    search_code,
    stop_watching,
)
from opencode_search.search import clear_search_cache
from opencode_search.watcher import watcher_manager

pytestmark = [pytest.mark.integration, pytest.mark.runtime_deps, pytest.mark.gpu]


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


async def _wait_for_search_result(
    project_root: Path,
    query: str,
    *,
    expected_substring: str,
    timeout_s: float = 15.0,
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        clear_search_cache()
        result = await search_code(
            query=query,
            project_paths=[str(project_root)],
            top_k=5,
            use_rerank=False,
        )
        if any(expected_substring in row.get("content", "") for row in result.get("results", [])):
            return result
        await asyncio.sleep(0.5)
    raise AssertionError(f"query {query!r} never returned content containing {expected_substring!r}")


async def _index_and_wait(path: str, timeout_s: float = 120.0, **kwargs) -> dict:
    """Call the index_project MCP tool and poll until the background task finishes."""
    result = await index_project(path=path, **kwargs)
    assert result["status"] == "indexing", f"expected 'indexing', got {result}"
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        st = await project_status(path=path)
        if st.get("indexed") and not st.get("indexing_running"):
            return st
        if not st.get("indexing_running") and st.get("indexed") is False:
            # Check _indexing_status directly for the error case
            from opencode_search.handlers import _indexing_status
            from pathlib import Path as _P
            ps = str(_P(path).expanduser().resolve())
            final = _indexing_status.get(ps, {})
            if final.get("status") == "error":
                return final
        await asyncio.sleep(0.5)
    raise AssertionError(f"indexing did not complete within {timeout_s}s")


async def _wait_for_search_absence(
    project_root: Path,
    query: str,
    *,
    absent_substring: str,
    timeout_s: float = 15.0,
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout_s
    last_result: dict | None = None
    while asyncio.get_running_loop().time() < deadline:
        clear_search_cache()
        result = await search_code(
            query=query,
            project_paths=[str(project_root)],
            top_k=5,
            use_rerank=False,
        )
        last_result = result
        if not any(absent_substring in row.get("content", "") for row in result.get("results", [])):
            return result
        await asyncio.sleep(0.5)
    raise AssertionError(
        f"query {query!r} kept returning content containing {absent_substring!r}: {last_result}"
    )


@pytest.mark.asyncio
async def test_mcp_tools_real_end_to_end(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    source_file = project_root / "app.py"
    source_file.write_text(
        "SMOKE_MCP_INITIAL = 'mcp_alpha_unique'\n"
        "def initial_token():\n"
        "    return SMOKE_MCP_INITIAL\n"
    )

    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(config, "REGISTRY_PATH", registry_path)

    await watcher_manager.stop_all()
    await resume_watchers()

    try:
        indexed = await _index_and_wait(str(project_root), tier="budget", watch=True)
        assert indexed.get("watching") is True

        status = await project_status(path=str(project_root))
        assert status["indexed"] is True
        assert status["watching"] is True

        listed = await list_indexed_projects()
        assert any(p["path"] == str(project_root) for p in listed["projects"])

        initial = await _wait_for_search_result(
            project_root,
            "mcp_alpha_unique",
            expected_substring="mcp_alpha_unique",
        )
        assert any("mcp_alpha_unique" in row["content"] for row in initial["results"])
        await asyncio.sleep(1.0)

        source_file.write_text(
            "SMOKE_MCP_UPDATED = 'mcp_beta_unique'\n"
            "def rotated_token():\n"
            "    return SMOKE_MCP_UPDATED\n"
        )
        updated = await _wait_for_search_result(
            project_root,
            "mcp_beta_unique",
            expected_substring="mcp_beta_unique",
        )
        assert any("mcp_beta_unique" in row["content"] for row in updated["results"])

        stopped = await stop_watching(path=str(project_root))
        assert stopped["status"] == "stopped"
        assert stopped["was_watching"] is True

        source_file.write_text(
            "SMOKE_MCP_STOPPED = 'mcp_gamma_unique'\n"
            "def stopped_token():\n"
            "    return SMOKE_MCP_STOPPED\n"
        )
        await asyncio.sleep(2.0)
        clear_search_cache()
        after_stop = await search_code(
            query="mcp_gamma_unique",
            project_paths=[str(project_root)],
            top_k=5,
            use_rerank=False,
        )
        assert not any(
            "mcp_gamma_unique" in row.get("content", "")
            for row in after_stop.get("results", [])
        )
    finally:
        await watcher_manager.stop_all()


@pytest.mark.asyncio
async def test_mcp_resumes_persisted_watcher_and_removes_deleted_files(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    source_file = project_root / "app.py"
    deleted_file = project_root / "delete_me.py"
    source_file.write_text(
        "SMOKE_MCP_RESUME_INITIAL = 'mcp_resume_alpha_unique'\n"
        "def resume_initial():\n"
        "    return SMOKE_MCP_RESUME_INITIAL\n"
    )
    deleted_file.write_text(
        "SMOKE_MCP_DELETE = 'mcp_delete_unique'\n"
        "def delete_token():\n"
        "    return SMOKE_MCP_DELETE\n"
    )

    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(config, "REGISTRY_PATH", registry_path)

    await watcher_manager.stop_all()
    await resume_watchers()

    try:
        indexed = await _index_and_wait(str(project_root), tier="budget", watch=True)
        assert indexed.get("watching") is True

        await _wait_for_search_result(
            project_root,
            "mcp_resume_alpha_unique",
            expected_substring="mcp_resume_alpha_unique",
        )
        await _wait_for_search_result(
            project_root,
            "mcp_delete_unique",
            expected_substring="mcp_delete_unique",
        )

        # Simulate the MCP process exiting: runtime watchers disappear, but the
        # persisted registry entry still has watch=True.
        await watcher_manager.stop_all()
        assert watcher_manager.list_active() == []

        await resume_watchers()
        status = await project_status(path=str(project_root))
        assert status["watching"] is True

        source_file.write_text(
            "SMOKE_MCP_RESUME_UPDATED = 'mcp_resume_beta_unique'\n"
            "def resume_updated():\n"
            "    return SMOKE_MCP_RESUME_UPDATED\n"
        )
        await _wait_for_search_result(
            project_root,
            "mcp_resume_beta_unique",
            expected_substring="mcp_resume_beta_unique",
        )

        deleted_file.unlink()
        await _wait_for_search_absence(
            project_root,
            "mcp_delete_unique",
            absent_substring="mcp_delete_unique",
        )

        stopped = await stop_watching(path=str(project_root))
        assert stopped["status"] == "stopped"
        assert stopped["was_watching"] is True
    finally:
        await watcher_manager.stop_all()


@pytest.mark.asyncio
async def test_mcp_first_use_index_auto_watch_and_background_release(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    nested = project_root / "nested"
    nested.mkdir(parents=True)
    source_file = project_root / "app.py"
    source_file.write_text(
        "SMOKE_MCP_FIRST_USE = 'mcp_first_use_unique'\n"
        "def first_use_token():\n"
        "    return SMOKE_MCP_FIRST_USE\n"
    )

    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(config, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr("opencode_search.mcp.DEFAULT_CLIENT_STALE_S", 1)

    await watcher_manager.stop_all()
    await resume_watchers()

    try:
        open_response = await client_open(
            _FakeRequest({"client_id": "client-a", "cwd": str(nested)})
        )
        assert open_response.status_code == 200

        await _index_and_wait(str(project_root), tier="budget", watch=False)

        status = await project_status(path=str(project_root))
        assert status["indexed"] is True
        assert status["watching"] is True
        assert str(project_root) in watcher_manager.list_active()

        await _wait_for_search_result(
            project_root,
            "mcp_first_use_unique",
            expected_substring="mcp_first_use_unique",
        )

        close_response = await client_close(_FakeRequest({"client_id": "client-a"}))
        assert close_response.status_code == 200
        assert str(project_root) in watcher_manager.list_active()

        deadline = asyncio.get_running_loop().time() + 6.0
        while asyncio.get_running_loop().time() < deadline:
            if str(project_root) not in watcher_manager.list_active():
                break
            await asyncio.sleep(0.2)
        else:
            raise AssertionError("auto-started watcher was not released after client disconnect")
    finally:
        await watcher_manager.stop_all()


@pytest.mark.asyncio
async def test_mcp_migrates_legacy_registry_db_path_before_resuming_watchers(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    source_file = project_root / "app.py"
    source_file.write_text(
        "SMOKE_MCP_LEGACY = 'mcp_legacy_unique'\n"
        "def legacy_token():\n"
        "    return SMOKE_MCP_LEGACY\n"
    )

    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(config, "REGISTRY_PATH", registry_path)

    await watcher_manager.stop_all()
    await resume_watchers()

    try:
        await _index_and_wait(str(project_root), tier="budget", watch=True)

        canonical_db_path = Path(config.get_project_db_path(project_root, "budget"))
        legacy_db_path = project_root / ".opencode" / "index_budget"
        canonical_db_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_db_path.parent.mkdir(parents=True, exist_ok=True)

        await watcher_manager.stop_all()
        assert watcher_manager.list_active() == []
        canonical_db_path.rename(legacy_db_path)

        registry_path.write_text(
            json.dumps(
                {
                    str(project_root): {
                        "path": str(project_root),
                        "db_path": str(legacy_db_path),
                        "tier": "budget",
                        "dims": config.get_tier_dims("budget"),
                        "watch": True,
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        await resume_watchers()

        status = await project_status(path=str(project_root))
        assert status["indexed"] is True
        assert status["watching"] is True
        assert Path(status["db_path"]) == canonical_db_path
        assert canonical_db_path.exists()
        assert not legacy_db_path.exists()

        resumed = await _wait_for_search_result(
            project_root,
            "mcp_legacy_unique",
            expected_substring="mcp_legacy_unique",
        )
        assert any("mcp_legacy_unique" in row["content"] for row in resumed["results"])
    finally:
        await watcher_manager.stop_all()
