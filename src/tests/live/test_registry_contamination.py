"""U2: registry contamination purge + hard-exclude guard.

Proves:
 1. is_registry_excluded() correctly identifies contaminated paths.
 2. The live registry contains no .venv/site-packages/node_modules//tmp roots.
 3. Attempting to index a contaminated path is rejected by handle_index_project.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# Unit-style (no daemon needed — just import + check logic)
# ---------------------------------------------------------------------------

class TestIsRegistryExcluded:
    """is_registry_excluded() must correctly classify paths — no I/O needed."""

    def _excl(self, path: str) -> bool:
        from opencode_search.discover import is_registry_excluded
        return is_registry_excluded(path)

    # --- paths that MUST be excluded ---

    def test_venv_excluded(self) -> None:
        assert self._excl("/home/user/git/myproject/.venv/lib/python3.12")

    def test_venv_root_excluded(self) -> None:
        assert self._excl("/home/user/git/myproject/.venv")

    def test_plain_venv_excluded(self) -> None:
        assert self._excl("/home/user/git/myproject/venv")

    def test_site_packages_excluded(self) -> None:
        assert self._excl("/home/user/git/myproject/.venv/lib/site-packages")

    def test_node_modules_excluded(self) -> None:
        assert self._excl("/home/user/git/myproject/node_modules/@babel/core")

    def test_tmp_excluded(self) -> None:
        assert self._excl("/tmp/pytest-of-user/pytest-42/test_foo0/my_project")

    def test_tmp_direct_excluded(self) -> None:
        assert self._excl("/tmp")

    def test_pycache_excluded(self) -> None:
        assert self._excl("/home/user/git/myproject/__pycache__")

    # --- paths that MUST be allowed ---

    def test_real_project_allowed(self) -> None:
        assert not self._excl("/home/user/git/github.com/fairyhunter13/opencode-search-engine")

    def test_go_project_allowed(self) -> None:
        assert not self._excl("/home/user/go/src/github.com/example-org/acme-ai-be")

    def test_home_project_allowed(self) -> None:
        assert not self._excl("/home/user/myapp")

    def test_env_in_name_allowed(self) -> None:
        # 'environment-config' contains 'env' but as a segment it's not '.env' or 'env'
        assert not self._excl("/home/user/git/environment-config")

    # --- widened protection from IGNORED_DIRS derivation (U2b) ---

    def test_go_pkg_mod_excluded(self) -> None:
        # 'pkg' is in IGNORED_DIRS; ~/go/pkg/mod must not become a registry root
        assert self._excl("/home/user/go/pkg/mod/github.com/some/library@v1.2.3")

    def test_vendor_excluded(self) -> None:
        # 'vendor' is in IGNORED_DIRS
        assert self._excl("/home/user/git/myproject/vendor")

    def test_derives_from_ignored_dirs(self) -> None:
        """U2b single-source contract: IGNORED_DIRS must be a subset of _REGISTRY_EXCLUDE_SEGMENTS
        and site-packages must be the only registry-specific addition."""
        from opencode_search.discover import (
            _REGISTRY_EXCLUDE_SEGMENTS,
            _REGISTRY_EXTRA_EXCLUDE_SEGMENTS,
            IGNORED_DIRS,
        )
        # Every segment in IGNORED_DIRS must be present in _REGISTRY_EXCLUDE_SEGMENTS
        missing = IGNORED_DIRS - _REGISTRY_EXCLUDE_SEGMENTS
        assert missing == frozenset(), (
            f"These IGNORED_DIRS segments are not in _REGISTRY_EXCLUDE_SEGMENTS: {missing}\n"
            f"IGNORED_DIRS must be a subset of _REGISTRY_EXCLUDE_SEGMENTS (U2b invariant)."
        )
        # site-packages is the only registry-specific extra beyond IGNORED_DIRS
        assert "site-packages" in _REGISTRY_EXCLUDE_SEGMENTS, (
            "site-packages must be in _REGISTRY_EXCLUDE_SEGMENTS"
        )
        assert "site-packages" in _REGISTRY_EXTRA_EXCLUDE_SEGMENTS, (
            "site-packages must be the sole extra segment in _REGISTRY_EXTRA_EXCLUDE_SEGMENTS"
        )


# ---------------------------------------------------------------------------
# Live: registry must be contamination-free
# ---------------------------------------------------------------------------

class TestLiveRegistryClean:
    """After a sweep, the live registry must contain no contaminated roots."""

    def test_no_contaminated_roots(self, http) -> None:
        """Registry must have zero .venv/site-packages/node_modules//tmp roots."""
        from opencode_search.discover import is_registry_excluded

        resp = http.get("/api/projects")
        assert resp.status_code == 200
        projects = resp.json().get("projects", [])
        assert projects, "No projects in registry — cannot run contamination check"

        contaminated = [
            p["path"] for p in projects
            if is_registry_excluded(p.get("path", ""))
        ]
        assert contaminated == [], (
            f"Registry contains contaminated roots: {contaminated}\n"
            f"Remove them via POST /api/remove_project and add the exclude guard."
        )

    def test_contaminated_index_request_rejected(self, http) -> None:
        """Attempting to index a /tmp path must be rejected (not queued)."""
        resp = http.post("/api/index", json={"path": "/tmp/definitely-a-test-fixture"})
        data = resp.json()
        # Either a 200 with status=rejected, or a 400/403/404 — anything except status=indexing
        assert data.get("status") != "indexing" and data.get("status") != "already_indexing", (
            f"Daemon accepted /tmp path for indexing: {data}"
        )
        # A clean rejection must carry an error message
        assert "error" in data or data.get("status") == "rejected", (
            f"Expected error/rejected in response; got: {data}"
        )

    def test_venv_index_request_rejected(self, http) -> None:
        """Attempting to index a .venv path must be rejected."""
        import os
        venv_path = os.path.join(
            "/home/user/git/github.com/fairyhunter13/opencode-search-engine",
            ".venv",
        )
        resp = http.post("/api/index", json={"path": venv_path})
        data = resp.json()
        assert data.get("status") != "indexing" and data.get("status") != "already_indexing", (
            f"Daemon accepted .venv path for indexing: {data}"
        )
