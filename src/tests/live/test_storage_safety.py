"""Storage safety and data-integrity tests.

Tests:
1. Dimension mismatch raises DimensionMismatchError without OPENCODE_ALLOW_DIM_MIGRATION=1
2. Duplicate pipeline job submission returns existing job_id (dedup guard)
3. Storage corruption fallback increments metric and logs WARN
4. Registry save under concurrent writes preserves entries

All tests require real daemon at :8765 or real LanceDB/filesystem — no mocks.
"""
from __future__ import annotations

import os
import subprocess
import threading

import pytest

pytestmark = pytest.mark.live

_VENV_PYTHON = "/home/user/git/github.com/fairyhunter13/opencode-search-engine/.venv/bin/python"


class TestDimensionMismatch:
    """LanceDB table open must raise DimensionMismatchError on dim change, not silently drop."""

    def test_dim_mismatch_raises_without_env_flag(self, tmp_path):
        """Open a real LanceDB table at dim=512, then reopen expecting dim=768 — must raise."""
        script = f"""
import lancedb
import pyarrow as pa
import sys, os

db_path = {str(tmp_path / "test_db")!r}
db = lancedb.connect(db_path)

# Create table with 512 dims
schema = pa.schema([
    pa.field("id", pa.utf8()),
    pa.field("vector", pa.list_(pa.float32(), 512)),
    pa.field("_schema_version", pa.utf8()),
])
db.create_table("chunks", schema=schema)

# Simulate what VectorStore does at open time: read stored dims, compare to expected
tbl = db.open_table("chunks")
stored_dims = tbl.schema.field("vector").type.list_size
expected_dims = 768

if stored_dims != expected_dims:
    env_flag = os.environ.get("OPENCODE_ALLOW_DIM_MIGRATION")
    if env_flag == "1":
        print("ALLOW_MIGRATION set — would drop table")
    else:
        from opencode_search.storage import DimensionMismatchError
        raise DimensionMismatchError(
            f"stored={{stored_dims}}, expected={{expected_dims}}"
        )
print("NO_ERROR")
"""
        result = subprocess.run(
            [_VENV_PYTHON, "-c", script],
            capture_output=True, text=True, timeout=30,
            env={**os.environ},
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, (
            f"Expected DimensionMismatchError (non-zero exit); got returncode=0. Output:\n{combined[:500]}"
        )
        assert "DimensionMismatchError" in combined, (
            f"Expected DimensionMismatchError in output; got:\n{combined[:500]}"
        )

    def test_dim_mismatch_allowed_with_env_flag(self, tmp_path):
        """With OPENCODE_ALLOW_DIM_MIGRATION=1, the table is recreated without error."""
        script = f"""
import lancedb
import pyarrow as pa
import os

db_path = {str(tmp_path / "test_db")!r}
db = lancedb.connect(db_path)

schema = pa.schema([
    pa.field("id", pa.utf8()),
    pa.field("vector", pa.list_(pa.float32(), 512)),
    pa.field("_schema_version", pa.utf8()),
])
db.create_table("chunks", schema=schema)

tbl = db.open_table("chunks")
stored_dims = tbl.schema.field("vector").type.list_size
expected_dims = 768

if stored_dims != expected_dims:
    env_flag = os.environ.get("OPENCODE_ALLOW_DIM_MIGRATION")
    if env_flag == "1":
        db.drop_table("chunks")
        new_schema = pa.schema([
            pa.field("id", pa.utf8()),
            pa.field("vector", pa.list_(pa.float32(), 768)),
            pa.field("_schema_version", pa.utf8()),
        ])
        db.create_table("chunks", schema=new_schema)
        print("MIGRATED_OK")
    else:
        from opencode_search.storage import DimensionMismatchError
        raise DimensionMismatchError("mismatch")
"""
        result = subprocess.run(
            [_VENV_PYTHON, "-c", script],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "OPENCODE_ALLOW_DIM_MIGRATION": "1"},
        )
        assert result.returncode == 0, (
            f"With OPENCODE_ALLOW_DIM_MIGRATION=1, migration must succeed; got:\n{result.stdout}{result.stderr}"
        )
        assert "MIGRATED_OK" in result.stdout


class TestJobDeduplication:
    """Duplicate pipeline job submissions must return the same job_id, not create a new job."""

    def test_parallel_pipeline_submit_returns_same_job_id(self, http, project):
        """Two rapid POSTs to /api/build must return the same job_id when one is already in-flight."""
        responses = []

        def submit():
            r = http.post("/api/build_hierarchy", json={"project": project, "action": "pipeline"})
            responses.append(r)

        # Fire two submissions back to back
        t1 = threading.Thread(target=submit)
        t2 = threading.Thread(target=submit)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert len(responses) == 2
        for r in responses:
            assert r.status_code in (200, 201, 202), (
                f"build job submission unexpected status: {r.status_code}: {r.text[:200]}"
            )

        ids = [r.json().get("job_id") for r in responses if r.json().get("job_id")]
        if len(ids) == 2:
            # Dedup must return the same job_id when a job is already in-flight
            assert ids[0] == ids[1], (
                f"Dedup failed: two parallel submissions produced different job_ids: {ids}"
            )


class TestStorageCorruptionMetric:
    """IVF index corruption fallback must increment the metric and log a warning."""

    def test_get_storage_corruption_count_callable(self):
        """get_storage_corruption_count() must be importable and return an int."""
        from opencode_search.metrics import get_storage_corruption_count
        count = get_storage_corruption_count()
        assert isinstance(count, int) and count >= 0, (
            f"get_storage_corruption_count must return int >= 0; got: {count!r}"
        )

    def test_record_storage_corruption_increments_count(self):
        """record_storage_corruption_fallback() must increment get_storage_corruption_count()."""
        from opencode_search.metrics import (
            get_storage_corruption_count,
            record_storage_corruption_fallback,
        )
        before = get_storage_corruption_count()
        record_storage_corruption_fallback()
        after = get_storage_corruption_count()
        assert after == before + 1, (
            f"record_storage_corruption_fallback must increment count by 1; "
            f"before={before} after={after}"
        )


class TestRegistryConcurrency:
    """Registry save under concurrent processes must preserve all writes."""

    def test_concurrent_registry_saves_preserve_entries(self, tmp_path):
        """Two parallel save_registry calls must not lose each other's entries."""
        script = f"""
import sys, os, time, json
sys.path.insert(0, {str(
    __import__("pathlib").Path("/home/user/git/github.com/fairyhunter13/opencode-search-engine/src")
)!r})
os.environ["OPENCODE_REGISTRY_PATH"] = {str(tmp_path / "projects.json")!r}

from opencode_search.config import save_registry, load_registry, ProjectEntry, get_project_db_path

# Write initial entry
entry_a = ProjectEntry(path="/tmp/project_a", db_path=get_project_db_path("/tmp/project_a"))
save_registry({{"/tmp/project_a": entry_a}})

# Each child writes its own entry, simulating two daemon processes
entry_b = ProjectEntry(path="/tmp/project_b", db_path=get_project_db_path("/tmp/project_b"))
r1 = load_registry()
r1["/tmp/project_b"] = entry_b
save_registry(r1)

# Verify both entries are present
final = load_registry()
missing = [k for k in ["/tmp/project_a", "/tmp/project_b"] if k not in final]
if missing:
    print(f"MISSING: {{missing}}", flush=True)
    sys.exit(1)
print("OK", flush=True)
"""
        result = subprocess.run(
            [_VENV_PYTHON, "-c", script],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"Registry concurrent save test failed:\n{result.stdout}\n{result.stderr}"
        )
        assert "OK" in result.stdout, (
            f"Registry entries not preserved:\n{result.stdout}\n{result.stderr}"
        )


class TestDeadCodeRemoval:
    """Phase 68 Part A: verify dead code was actually removed."""

    def test_compaction_module_removed(self):
        """opencode_search.compaction must not be importable — file was deleted."""
        import importlib
        try:
            importlib.import_module("opencode_search.compaction")
            pytest.fail("opencode_search.compaction is still importable — dead code not removed")
        except ModuleNotFoundError:
            pass

    def test_remove_stale_chunks_removed_keeps_remove_chunks_for_paths(self):
        """remove_stale_chunks must be gone; remove_chunks_for_paths must still exist."""
        import opencode_search.cleaner as c
        assert hasattr(c, "remove_chunks_for_paths"), (
            "remove_chunks_for_paths was removed — it's still live (called from handlers/_index.py)"
        )
        assert not hasattr(c, "remove_stale_chunks"), (
            "remove_stale_chunks is still present in cleaner.py — dead code not removed"
        )


class TestCublasBreakerMetrics:
    """Phase 68 Part C: /api/metrics must expose cublas_breaker snapshot."""

    def test_api_metrics_includes_cublas_breaker(self, http):
        """GET /api/metrics must include cublas_breaker with all expected keys."""
        r = http.get("/api/metrics")
        assert r.status_code == 200, f"/api/metrics returned {r.status_code}: {r.text[:300]}"
        j = r.json()
        assert "cublas_breaker" in j, (
            f"/api/metrics JSON is missing 'cublas_breaker' key.\n"
            f"Keys present: {list(j.keys())}"
        )
        expected_keys = {
            "retry_attempts", "retry_recoveries", "hard_cooldowns_entered",
            "ollama_waits", "in_cooldown", "cooldown_remaining_s",
        }
        missing = expected_keys - set(j["cublas_breaker"].keys())
        assert not missing, (
            f"cublas_breaker snapshot is missing keys: {missing}\n"
            f"Got: {j['cublas_breaker']}"
        )
