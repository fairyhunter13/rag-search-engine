"""Live test: file watcher is active and detects changes.

Requires: daemon at :8765, an indexed project with watching=true.
"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.live


class TestFileWatcher:
    def test_watcher_is_active(self, http, project):
        """Daemon reports watching=true for the indexed project."""
        r = http.get("/api/projects")
        assert r.status_code == 200
        projects = r.json().get("projects", [])
        entry = next((p for p in projects if p["path"] == project), None)
        assert entry is not None, f"Project {project} not in /api/projects"
        assert entry.get("watching") is True, (
            f"Expected watching=True for {project}, got {entry.get('watching')!r}. "
            "Watcher may have stopped — check daemon logs."
        )

    @pytest.mark.slow
    def test_watcher_detects_new_file(self, http, project, tmp_path):
        """Writing a new .py file triggers reindex within 60 seconds.

        Strategy: record current indexed_at from /api/projects, write a probe
        file into the project, poll until indexed_at changes or timeout.
        indexed_at lives in /api/projects (not /api/kb_health).
        """
        import pathlib

        def _get_indexed_at() -> str:
            r = http.get("/api/projects")
            if r.status_code != 200:
                return ""
            entry = next((p for p in r.json().get("projects", []) if p["path"] == project), {})
            return entry.get("indexed_at") or ""

        before_time = _get_indexed_at()

        # Write a probe file into the project root
        probe_path = pathlib.Path(project) / ".opencode_watcher_probe.py"
        probe_path.write_text("# watcher probe\n_x = 1\n")

        try:
            deadline = time.time() + 60
            changed = False
            while time.time() < deadline:
                time.sleep(5)
                after_time = _get_indexed_at()
                if after_time and after_time != before_time:
                    changed = True
                    break
            assert changed, (
                f"indexed_at did not change within 60 s after writing probe file. "
                f"before={before_time!r}. Watcher may not be detecting changes."
            )
        finally:
            probe_path.unlink(missing_ok=True)

    def test_watcher_project_has_communities(self, http, project):
        """Sanity: indexed project has >0 communities (confirms index is alive)."""
        r = http.get(f"/api/kb_health?project={project}")
        assert r.status_code == 200
        data = r.json()
        total = data.get("total_communities", 0)
        assert total > 0, f"Expected communities > 0 for {project}, got {total}"
