"""Tests for opencode_search.cli — typer command dispatch with mocked handlers."""
# ruff: noqa: E402
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("typer")
pytest.importorskip("typer.testing")

from typer.testing import CliRunner

from opencode_search.cli import app

pytestmark = [pytest.mark.integration, pytest.mark.runtime_deps]

runner = CliRunner()


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------


def test_index_invalid_tier(tmp_path):
    fake_result = {"error": "Invalid tier 'bogus'. Choose: ['balanced', 'budget', 'premium']"}
    with patch("opencode_search.handlers.handle_index_project",
               AsyncMock(return_value=fake_result)):
        result = runner.invoke(app, ["index", str(tmp_path), "--tier", "bogus"])
    assert result.exit_code == 1
    assert "Error" in result.output


def test_index_success(tmp_path):
    fake_result = {
        "status": "ok",
        "path": str(tmp_path),
        "tier": "balanced",
        "files_indexed": 5,
        "files_unchanged": 2,
        "chunks_total": 25,
        "errors": 0,
        "elapsed_s": 1.2,
        "watching": False,
    }
    with patch("opencode_search.handlers.handle_index_project",
               AsyncMock(return_value=fake_result)):
        result = runner.invoke(app, ["index", str(tmp_path)])
    assert result.exit_code == 0
    assert "Indexed" in result.output
    assert "5" in result.output


def test_index_json_output(tmp_path):
    fake_result = {
        "status": "ok", "path": str(tmp_path), "tier": "budget",
        "files_indexed": 1, "chunks_total": 3, "errors": 0,
        "elapsed_s": 0.1, "watching": False,
    }
    with patch("opencode_search.handlers.handle_index_project",
               AsyncMock(return_value=fake_result)):
        result = runner.invoke(app, ["index", str(tmp_path), "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["status"] == "ok"
    assert parsed["files_indexed"] == 1


def test_index_with_force_and_watch(tmp_path):
    captured = {}

    async def capture(**kwargs):
        captured.update(kwargs)
        return {
            "status": "ok", "path": str(tmp_path), "tier": "premium",
            "files_indexed": 0, "chunks_total": 0, "errors": 0,
            "elapsed_s": 0.0, "watching": True,
        }

    with patch("opencode_search.handlers.handle_index_project", side_effect=capture):
        result = runner.invoke(app, [
            "index", str(tmp_path), "--tier", "premium", "--force", "--watch"
        ])

    assert result.exit_code == 0
    assert captured.get("tier") == "premium"
    assert captured.get("force") is True
    assert captured.get("watch") is True


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_no_results():
    fake = {"results": [], "elapsed_ms": 12.0, "query": "x", "projects_searched": 1}
    with patch("opencode_search.handlers.handle_search_code",
               AsyncMock(return_value=fake)):
        result = runner.invoke(app, ["search", "find x"])
    assert result.exit_code == 0
    assert "No results" in result.output


def test_search_with_results():
    fake = {
        "results": [
            {
                "path": "/tmp/foo.py", "content": "def foo(): pass",
                "language": "python", "start_line": 1, "end_line": 2,
                "score": 0.92, "project_path": "/tmp",
            }
        ],
        "elapsed_ms": 45.0,
        "query": "find foo",
        "projects_searched": 1,
    }
    with patch("opencode_search.handlers.handle_search_code",
               AsyncMock(return_value=fake)):
        result = runner.invoke(app, ["search", "find foo"])
    assert result.exit_code == 0
    assert "/tmp/foo.py" in result.output
    assert "0.9200" in result.output


def test_search_json_output():
    fake = {"results": [], "elapsed_ms": 0.0, "query": "x", "projects_searched": 0}
    with patch("opencode_search.handlers.handle_search_code",
               AsyncMock(return_value=fake)):
        result = runner.invoke(app, ["search", "x", "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert "results" in parsed


def test_search_no_rerank_flag():
    captured = {}

    async def capture(**kwargs):
        captured.update(kwargs)
        return {"results": [], "elapsed_ms": 0.0, "query": "x", "projects_searched": 0}

    with patch("opencode_search.handlers.handle_search_code", side_effect=capture):
        runner.invoke(app, ["search", "x", "--no-rerank"])

    assert captured.get("use_rerank") is False


def test_search_with_project_filter():
    captured = {}

    async def capture(**kwargs):
        captured.update(kwargs)
        return {"results": [], "elapsed_ms": 0.0, "query": "x", "projects_searched": 0}

    with patch("opencode_search.handlers.handle_search_code", side_effect=capture):
        runner.invoke(app, ["search", "x", "-p", "/tmp/a", "-p", "/tmp/b"])

    assert captured.get("project_paths") == ["/tmp/a", "/tmp/b"]


def test_search_error_response():
    with patch("opencode_search.handlers.handle_search_code",
               AsyncMock(return_value={"error": "Query must not be empty"})):
        result = runner.invoke(app, ["search", ""])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# status / list
# ---------------------------------------------------------------------------


def test_status_with_path_indexed():
    fake = {
        "indexed": True, "path": "/tmp/proj", "tier": "balanced",
        "db_path": "/tmp/proj/.opencode/index_balanced", "chunks": 42,
        "watching": False, "indexed_at": "2026-01-01T00:00:00",
    }
    with patch("opencode_search.handlers.handle_project_status",
               AsyncMock(return_value=fake)):
        result = runner.invoke(app, ["status", "/tmp/proj"])
    assert result.exit_code == 0
    assert "balanced" in result.output
    assert "42" in result.output


def test_status_with_path_not_indexed():
    with patch("opencode_search.handlers.handle_project_status",
               AsyncMock(return_value={"indexed": False, "path": "/tmp/unknown"})):
        result = runner.invoke(app, ["status", "/tmp/unknown"])
    assert result.exit_code == 0
    assert "Not indexed" in result.output


def test_status_no_path_lists_all():
    fake = {"projects": [
        {"path": "/tmp/a", "tier": "balanced", "db_path": "/tmp/a/db",
         "watching": True, "indexed_at": "2026-01-01"},
    ]}
    with patch("opencode_search.handlers.handle_list_indexed_projects",
               AsyncMock(return_value=fake)):
        result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "/tmp/a" in result.output


def test_list_empty():
    with patch("opencode_search.handlers.handle_list_indexed_projects",
               AsyncMock(return_value={"projects": []})):
        result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "No indexed projects" in result.output


def test_list_with_projects():
    fake = {"projects": [
        {"path": "/tmp/x", "tier": "premium", "db_path": "/tmp/x/db",
         "watching": False, "indexed_at": "2026-01-01"},
    ]}
    with patch("opencode_search.handlers.handle_list_indexed_projects",
               AsyncMock(return_value=fake)):
        result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "/tmp/x" in result.output
    assert "premium" in result.output


# ---------------------------------------------------------------------------
# stop-watching
# ---------------------------------------------------------------------------


def test_stop_watching_was_active():
    fake = {"path": "/tmp/x", "was_watching": True, "status": "stopped"}
    with patch("opencode_search.handlers.handle_stop_watching",
               AsyncMock(return_value=fake)):
        result = runner.invoke(app, ["stop-watching", "/tmp/x"])
    assert result.exit_code == 0
    assert "Stopped watcher" in result.output


def test_stop_watching_not_active():
    fake = {"path": "/tmp/x", "was_watching": False, "status": "stopped"}
    with patch("opencode_search.handlers.handle_stop_watching",
               AsyncMock(return_value=fake)):
        result = runner.invoke(app, ["stop-watching", "/tmp/x"])
    assert result.exit_code == 0
    assert "No active watcher" in result.output


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


def test_health_command_runs():
    """Health command exits 0 or 1 — never crashes."""
    result = runner.invoke(app, ["health"])
    # exit_code is 0 if GPU OK, 1 otherwise — both acceptable
    assert result.exit_code in (0, 1)
    # Should print something about GPU
    assert "GPU" in result.output


def test_health_json_output():
    result = runner.invoke(app, ["health", "--json"])
    # Should produce valid JSON regardless of GPU status
    try:
        data = json.loads(result.output)
    except json.JSONDecodeError:
        pytest.fail(f"health --json did not produce valid JSON; got: {result.output[:300]}")
    assert "gpu_ok" in data


# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------


def test_daemon_ensure_json_output():
    fake = {"status": "already_running", "url": "http://127.0.0.1:8765/mcp"}
    with patch("opencode_search.daemon.ensure_daemon_running", return_value=fake):
        result = runner.invoke(app, ["daemon", "ensure", "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["status"] == "already_running"


def test_daemon_install_global_success():
    fake = {"status": "ok", "url": "http://127.0.0.1:8765/mcp"}
    with patch("opencode_search.daemon.install_global_integration", return_value=fake):
        result = runner.invoke(app, ["daemon", "install-global"])
    assert result.exit_code == 0
    assert "Installed global MCP integration" in result.output


def test_daemon_install_systemd_success():
    fake = {"installed": True, "service_path": "/tmp/opencode-search-mcp-daemon.service"}
    with patch("opencode_search.daemon.install_systemd_user_service", return_value=fake):
        result = runner.invoke(app, ["daemon", "install-systemd"])
    assert result.exit_code == 0
    assert "Installed systemd user service" in result.output


# ---------------------------------------------------------------------------
# Help / no-args
# ---------------------------------------------------------------------------


def test_no_args_shows_help():
    result = runner.invoke(app, [])
    # No-args should show help (exit code 0 or 2 depending on typer version)
    assert "index" in result.output
    assert "search" in result.output


def test_help_lists_all_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ["index", "search", "status", "list", "watch", "stop-watching", "mcp", "daemon", "health"]:
        assert cmd in result.output
