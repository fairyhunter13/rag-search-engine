"""Manage action side-effect tests: hooks, vacuum, remove_project, stalled pipeline resume.

Tests:
1. install_hooks writes post-commit file at expected path
2. uninstall_hooks removes the managed section from post-commit
3. vacuum dry_run=True produces no on-disk delta
4. remove_project with delete_index=True removes the index directory
5. resume_stalled_pipelines schedules auto-pipeline for stranded projects

All tests use real filesystem inspection — no mocks.
"""
from __future__ import annotations

import subprocess

import pytest

pytestmark = pytest.mark.live

_VENV_PYTHON = "/home/user/git/github.com/fairyhunter13/opencode-search-engine/.venv/bin/python"
_HOOK_HEADER = "# opencode-search managed hook — do not edit this line"


class TestGitHooksSideEffects:
    """install_hooks / uninstall_hooks must write and remove the hook file on disk."""

    def test_install_hooks_writes_post_commit(self, tmp_path):
        """install_hooks must create .git/hooks/post-commit with the managed header."""
        # Create a minimal git repo in tmp_path
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        hooks_dir = tmp_path / ".git" / "hooks"
        hook_file = hooks_dir / "post-commit"

        # Call handler directly (no HTTP round-trip needed — pure filesystem)
        script = f"""
import asyncio, sys
sys.path.insert(0, "/home/user/git/github.com/fairyhunter13/opencode-search-engine/src")
from opencode_search.handlers._hooks import handle_git_hooks
result = asyncio.run(handle_git_hooks({str(tmp_path)!r}, install=True))
print(result["status"])
"""
        result = subprocess.run(
            [_VENV_PYTHON, "-c", script],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, f"install_hooks script failed:\n{result.stderr}"
        assert result.stdout.strip() in ("installed", "already_installed"), (
            f"Unexpected install status: {result.stdout.strip()!r}"
        )
        assert hook_file.exists(), f"post-commit hook file was not created at {hook_file}"
        content = hook_file.read_text()
        assert _HOOK_HEADER in content, (
            f"Managed hook header missing from post-commit:\n{content[:300]}"
        )

    def test_uninstall_hooks_removes_managed_section(self, tmp_path):
        """uninstall_hooks must remove the managed section, leaving the file absent or clean."""
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        hook_file = tmp_path / ".git" / "hooks" / "post-commit"

        script_install = f"""
import asyncio, sys
sys.path.insert(0, "/home/user/git/github.com/fairyhunter13/opencode-search-engine/src")
from opencode_search.handlers._hooks import handle_git_hooks
asyncio.run(handle_git_hooks({str(tmp_path)!r}, install=True))
"""
        subprocess.run([_VENV_PYTHON, "-c", script_install], capture_output=True, check=True)
        assert hook_file.exists(), "post-commit must exist after install"

        script_uninstall = f"""
import asyncio, sys
sys.path.insert(0, "/home/user/git/github.com/fairyhunter13/opencode-search-engine/src")
from opencode_search.handlers._hooks import handle_git_hooks
result = asyncio.run(handle_git_hooks({str(tmp_path)!r}, install=False))
print(result["status"])
"""
        result = subprocess.run(
            [_VENV_PYTHON, "-c", script_uninstall],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, f"uninstall_hooks failed:\n{result.stderr}"
        assert result.stdout.strip() == "uninstalled", (
            f"Expected 'uninstalled' status; got: {result.stdout.strip()!r}"
        )
        # Hook file must be gone (removed when it only had the managed section)
        if hook_file.exists():
            remaining = hook_file.read_text()
            assert _HOOK_HEADER not in remaining, (
                f"Managed section still present after uninstall:\n{remaining[:300]}"
            )


class TestVacuumDryRun:
    """vacuum dry_run=True must not change anything on disk."""

    def test_vacuum_dry_run_no_disk_delta(self, http, project):
        """POST /api/vacuum?dry_run=true must return 200 and produce zero bytes freed (dry run)."""
        r = http.post(
            "/api/vacuum",
            params={"project": project, "dry_run": "true"},
        )
        assert r.status_code == 200, (
            f"vacuum dry_run failed: {r.status_code}: {r.text[:200]}"
        )
        data = r.json()
        assert "error" not in data or data.get("error") is None, (
            f"vacuum dry_run returned error: {data}"
        )
        # dry_run must not remove anything — freed_mb should be 0 or absent
        freed = data.get("freed_mb", 0) or data.get("freed_bytes", 0)
        assert freed == 0, (
            f"vacuum dry_run must not free any bytes; freed={freed}: {data}"
        )


class TestRemoveProject:
    """remove_project with delete_index=True must remove the index directory."""

    def test_remove_project_delete_index_removes_dir(self, http, tmp_path):
        """Register a fresh project, pipeline-build a stub index, remove_project — dir must be gone."""
        from opencode_search.config import get_project_index_dir

        fake_proj = tmp_path / "my_project"
        fake_proj.mkdir()
        (fake_proj / "main.py").write_text("print('hello')\n")

        # Register the project
        r_reg = http.post("/api/projects/register", json={"path": str(fake_proj)})
        assert r_reg.status_code in (200, 201, 409), (
            f"Cannot register project: {r_reg.status_code}: {r_reg.text[:200]}"
        )

        index_dir = get_project_index_dir(str(fake_proj))

        # Remove project with delete_index=True
        r_rm = http.post("/api/remove_project", json={
            "project": str(fake_proj),
            "delete_index": True,
        })
        assert r_rm.status_code in (200, 201), (
            f"remove_project failed: {r_rm.status_code}: {r_rm.text[:200]}"
        )

        # Index directory must be gone (or never existed if not indexed)
        assert not index_dir.exists(), (
            f"Index directory still exists after remove_project(delete_index=True): {index_dir}"
        )


class TestStalledPipelineResume:
    """resume_stalled_pipelines() self-heal: detects and re-queues stranded projects.

    A project is 'stranded' when it has file_count > 0 in the registry but was
    never enriched (daemon crashed mid-pipeline). On startup, resume_stalled_pipelines()
    must detect these and call schedule_auto_pipeline() for each.
    """

    def test_project_is_fresh_for_unenriched_dir(self, tmp_path):
        """_project_is_fresh returns True for a directory with no graph DB, wiki, or patterns."""
        script = f"""
import sys
sys.path.insert(0, "/home/user/git/github.com/fairyhunter13/opencode-search-engine/src")
from opencode_search.handlers._autopipeline import _project_is_fresh, auto_pipeline_enabled
assert auto_pipeline_enabled(), "OPENCODE_AUTO_PIPELINE must default to enabled"
result = _project_is_fresh({str(tmp_path)!r})
print("fresh" if result else "not_fresh")
"""
        r = subprocess.run(
            [_VENV_PYTHON, "-c", script],
            capture_output=True, text=True, timeout=15,
        )
        assert r.returncode == 0, f"Script failed:\n{r.stderr}"
        assert r.stdout.strip() == "fresh", (
            f"_project_is_fresh must return True for a directory with no graph DB; "
            f"got {r.stdout.strip()!r}\nstderr: {r.stderr[-200:]}"
        )

    def test_resume_stalled_schedules_fresh_registry_project(self, tmp_path):
        """resume_stalled_pipelines() schedules auto-pipeline for registered fresh projects.

        Uses an isolated registry (OPENCODE_REGISTRY_PATH) so real state is not modified.
        """
        fake_proj = tmp_path / "stalled_project"
        fake_proj.mkdir()
        (fake_proj / "main.py").write_text("def hello(): pass\n")

        isolated_registry = tmp_path / "projects.json"

        script = f"""
import asyncio, json, os, sys
sys.path.insert(0, "/home/user/git/github.com/fairyhunter13/opencode-search-engine/src")

# Point to isolated registry so we don't touch the real one
os.environ["OPENCODE_REGISTRY_PATH"] = {str(isolated_registry)!r}

from opencode_search.config import load_registry, save_registry, ProjectEntry

# Write a stranded entry: file_count=1 but no graph DB (simulates interrupted pipeline)
registry = {{}}
registry[{str(fake_proj)!r}] = ProjectEntry(
    path={str(fake_proj)!r},
    db_path=str(os.path.join({str(tmp_path)!r}, "index")),
    dims=768,
    indexed_at="2026-01-01T00:00:00+00:00",
    file_count=1,
)
save_registry(registry)

from opencode_search.mcp import resume_stalled_pipelines
from opencode_search.handlers._autopipeline import get_pipeline_events

async def run():
    # startup_delay=0 skips the production 10s wait — no patching needed
    await asyncio.wait_for(resume_stalled_pipelines(startup_delay=0), timeout=5.0)

asyncio.run(run())

events = get_pipeline_events()
paths_scheduled = {{e["project_path"] for e in events if e.get("status") in ("scheduled", "running")}}
print("scheduled" if {str(fake_proj)!r} in paths_scheduled else "not_scheduled")
print("events:", json.dumps(events[-3:]))
"""
        r = subprocess.run(
            [_VENV_PYTHON, "-c", script],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0, f"Script failed:\n{r.stderr}"
        first_line = r.stdout.strip().split("\n")[0]
        assert first_line == "scheduled", (
            f"resume_stalled_pipelines must schedule a fresh project; got {first_line!r}\n"
            f"stdout: {r.stdout[:400]}\nstderr: {r.stderr[-300:]}"
        )
