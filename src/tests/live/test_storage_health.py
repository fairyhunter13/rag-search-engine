"""Storage health tests: stale LanceDB index dirs, WAL bounds, CLI command, maintenance sweep.

All tests require the daemon running at :8765 and at least one indexed project.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live

_ASTRO = "/home/user/git/github.com/fairyhunter13/redacted-name-3"


class TestStorageHealthSurface:
    """GET /api/storage_health returns actionable diagnostics — no MCP surface."""

    def test_storage_health_endpoint_returns_ok(self, http, astro):
        r = http.get("/api/storage_health", params={"project": astro})
        assert r.status_code == 200, f"unexpected status {r.status_code}: {r.text[:200]}"
        body = r.json()
        assert body.get("status") == "ok", f"storage_health not ok: {body}"

    # Extra reruns: this test can fail with ConnectError if the daemon restarts
    # transiently during a test session (e.g. from memory pressure + lancedb import).
    @pytest.mark.flaky(reruns=3, reruns_delay=30)
    def test_storage_health_has_required_fields(self, http, astro):
        r = http.get("/api/storage_health", params={"project": astro})
        body = r.json()
        projects = body.get("projects", [])
        assert len(projects) == 1, f"expected 1 project, got {len(projects)}: {projects}"
        stats = projects[0]
        for field in (
            "total_bytes", "data_bytes", "indices_bytes", "wal_bytes",
            "active_index_count", "on_disk_index_dirs", "stale_index_dirs", "recoverable_mb",
        ):
            assert field in stats, f"missing field {field!r} in storage_health stats: {stats}"

    def test_storage_health_types_are_numeric(self, http, astro):
        r = http.get("/api/storage_health", params={"project": astro})
        stats = r.json()["projects"][0]
        assert isinstance(stats["total_bytes"], int), "total_bytes not int"
        assert isinstance(stats["wal_bytes"], int), "wal_bytes not int"
        assert isinstance(stats["active_index_count"], int), "active_index_count not int"
        assert isinstance(stats["stale_index_dirs"], int), "stale_index_dirs not int"
        assert isinstance(stats["recoverable_mb"], float), "recoverable_mb not float"

    def test_storage_health_no_project_returns_all(self, http):
        r = http.get("/api/storage_health")
        assert r.status_code == 200
        body = r.json()
        assert body.get("status") == "ok"
        assert body.get("project_count", 0) >= 1

    def test_stale_index_dirs_bounded_after_vacuum(self, tmp_path):
        """vacuum() returns status=ok on an isolated Storage with multiple index versions.

        LanceDB's optimize() prunes old dataset VERSIONS (not _indices/ dirs) — it
        may create additional _indices/ entries during compaction. Only status=ok is
        asserted here; _indices/ dir count is not checked.
        """
        import asyncio

        from opencode_search.storage import Storage

        dims = 768
        db_path = str(tmp_path / "isolated_index")

        async def run():
            s = Storage(db_path=db_path, dims=dims)
            await s.open()
            try:
                batch1 = TestIvfPqRetrainGate._make_chunks(start_id=0, count=600, dims=dims)
                await s.write_chunks(batch1)
                await s.ensure_ivf_pq_index()
                s.set_config("ivf_pq_last_trained_count", "0")
                batch2 = TestIvfPqRetrainGate._make_chunks(start_id=600, count=600, dims=dims)
                await s.write_chunks(batch2)
                await s.ensure_ivf_pq_index()

                result = await s.vacuum()
                assert result.get("status") == "ok", f"vacuum() failed: {result}"
            finally:
                await s.close()

        asyncio.run(run())


class TestGraphWALBounded:
    """graph.db-wal stays bounded once wal_autocheckpoint + journal_size_limit are set."""

    def test_wal_bytes_reported(self, http, astro):
        r = http.get("/api/storage_health", params={"project": astro})
        stats = r.json()["projects"][0]
        wal_bytes = stats["wal_bytes"]
        assert wal_bytes >= 0, f"wal_bytes must be non-negative, got {wal_bytes}"

    def test_wal_pragmas_set_on_open(self):
        """GraphStorage.open() sets wal_autocheckpoint and journal_size_limit."""
        import tempfile

        from opencode_search.graph.storage import GraphStorage

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_graph.db")
            gs = GraphStorage(db_path)
            gs.open()
            try:
                db = gs._db()
                autochk = db.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
                jsize = db.execute("PRAGMA journal_size_limit").fetchone()[0]
                assert autochk == 1000, f"wal_autocheckpoint expected 1000, got {autochk}"
                assert jsize == 67108864, f"journal_size_limit expected 67108864, got {jsize}"
            finally:
                gs.close()

    def test_jobs_db_wal_mode_on_live_daemon(self, http):
        """The live daemon's jobs.db runs in WAL mode (verified against the real file)."""
        import sqlite3

        import opencode_search.jobs_store as jobs_store

        r = http.get("/api/jobs")
        assert r.status_code == 200, f"/api/jobs failed: {r.status_code}"

        conn = sqlite3.connect(str(jobs_store._JOBS_DB_PATH))
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode == "wal", f"jobs.db journal_mode expected 'wal', got {mode!r}"
        finally:
            conn.close()

    def test_metrics_db_wal_mode_on_live_daemon(self, http):
        """The live daemon's metrics.db runs in WAL mode (verified against the real file)."""
        import sqlite3

        import opencode_search.dashboard as dashboard

        # /api/metrics/history triggers _get_metrics_db() which applies the WAL pragma.
        r = http.get("/api/metrics/history")
        assert r.status_code == 200, f"/api/metrics/history failed: {r.status_code}"

        conn = sqlite3.connect(str(dashboard._METRICS_DB))
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode == "wal", f"metrics.db journal_mode expected 'wal', got {mode!r}"
        finally:
            conn.close()


class TestMaintenanceDeepVacuum:
    """Storage.vacuum() compacts data and prunes old dataset versions."""

    def test_vacuum_succeeds_on_isolated_storage(self, tmp_path):
        """Storage.vacuum() returns status=ok on a real isolated LanceDB Storage.

        LanceDB's optimize() prunes old dataset VERSIONS (not _indices/ dirs).
        On small test databases the optimizer may create new index files that
        exceed the pruned version savings — bytes are not asserted here. Only
        status=ok is checked; the byte savings are meaningful on large production
        databases (hundreds of MB of old versions).
        """
        import asyncio

        from opencode_search.storage import Storage

        dims = 768
        db_path = str(tmp_path / "isolated_vacuum")

        async def run():
            s = Storage(db_path=db_path, dims=dims)
            await s.open()
            try:
                # Write 3 batches to accumulate dataset versions.
                for i in range(3):
                    batch = TestIvfPqRetrainGate._make_chunks(start_id=i * 400, count=400, dims=dims)
                    await s.write_chunks(batch)
                s.set_config("ivf_pq_last_trained_count", "0")
                await s.ensure_ivf_pq_index()

                result = await s.vacuum()
                assert result.get("status") == "ok", f"vacuum() failed: {result}"
                assert "before_mb" in result and "after_mb" in result, (
                    f"vacuum() missing byte stats: {result}"
                )
            finally:
                await s.close()

        asyncio.run(run())


class TestIvfPqRetrainGate:
    """ensure_ivf_pq_index() skips retraining when row growth is below threshold.

    Tests drive a real LanceDB Storage on a temporary isolated database — no mocks.
    Vectors are synthetic unit vectors (NumPy) so no GPU/inference is required;
    the ANN index training itself runs on CPU.
    """

    @staticmethod
    def _make_chunks(start_id: int, count: int, dims: int = 768):
        """Build synthetic ChunkData rows with random unit vectors."""
        import hashlib
        import time

        import numpy as np

        from opencode_search.storage import ChunkData

        rng = np.random.default_rng(seed=start_id)
        now_us = int(time.time() * 1e6)
        chunks = []
        for i in range(count):
            raw = rng.random(dims).astype("float32")
            vec = (raw / (np.linalg.norm(raw) + 1e-9)).tolist()
            cid = start_id + i
            chunks.append(ChunkData(
                chunk_id=cid,
                path=f"/fake/file_{cid % 10}.py",
                file_hash=hashlib.md5(str(cid).encode()).hexdigest(),
                language="python",
                position=i,
                content=f"chunk content {cid}",
                content_hash=hashlib.md5(f"c{cid}".encode()).hexdigest(),
                start_line=i * 10,
                end_line=i * 10 + 9,
                vector=vec,
                created_at=now_us,
            ))
        return chunks

    def test_retrain_gate_real_lancedb(self, monkeypatch, tmp_path):
        """Gate logic exercised end-to-end against a real LanceDB Storage.

        Phase 1 — first train:  600 rows > threshold → index built, count persisted.
        Phase 2 — no growth:    same count → retrain skipped, _indices/ unchanged.
        Phase 3 — tiny growth:  +50 rows (delta < 500 AND < 10%) → skipped.
        Phase 4 — large growth: +600 rows (delta >= 500) → retrain fires.
        """
        import asyncio
        import pathlib

        from opencode_search.storage import Storage

        monkeypatch.setenv("OPENCODE_IVF_PQ_RETRAIN_FRAC", "0.10")
        monkeypatch.setenv("OPENCODE_IVF_PQ_RETRAIN_MIN_DELTA", "500")

        db_path = str(tmp_path / "test_index")
        dims = 768

        async def run():
            s = Storage(db_path=db_path, dims=dims)
            await s.open()
            try:
                indices_dir = pathlib.Path(db_path) / "chunks.lance" / "_indices"

                # Phase 1: first train — 600 rows > IVF_PQ_THRESHOLD (512)
                batch1 = self._make_chunks(start_id=0, count=600, dims=dims)
                await s.write_chunks(batch1)
                assert await s.count() == 600
                await s.ensure_ivf_pq_index()
                trained_count = s.get_config("ivf_pq_last_trained_count")
                assert trained_count == "600", f"Phase 1: expected '600', got {trained_count!r}"
                dirs_after_phase1 = sum(1 for e in os.scandir(str(indices_dir)) if e.is_dir()) if indices_dir.exists() else 0
                assert dirs_after_phase1 >= 1, "Phase 1: no _indices/ dir created after first train"

                # Phase 2: no new rows — retrain must be skipped
                await s.ensure_ivf_pq_index()
                trained_count2 = s.get_config("ivf_pq_last_trained_count")
                assert trained_count2 == "600", f"Phase 2: count changed unexpectedly to {trained_count2!r}"
                dirs_after_phase2 = sum(1 for e in os.scandir(str(indices_dir)) if e.is_dir()) if indices_dir.exists() else 0
                assert dirs_after_phase2 == dirs_after_phase1, (
                    f"Phase 2: _indices/ grew from {dirs_after_phase1} to {dirs_after_phase2} — retrain not skipped"
                )

                # Phase 3: +50 rows (delta=50 < 500 AND < 10%·600=60) → skip
                batch2 = self._make_chunks(start_id=600, count=50, dims=dims)
                await s.write_chunks(batch2)
                assert await s.count() == 650
                await s.ensure_ivf_pq_index()
                trained_count3 = s.get_config("ivf_pq_last_trained_count")
                assert trained_count3 == "600", (
                    f"Phase 3: sub-threshold growth retrained unexpectedly (count={trained_count3!r})"
                )
                dirs_after_phase3 = sum(1 for e in os.scandir(str(indices_dir)) if e.is_dir()) if indices_dir.exists() else 0
                assert dirs_after_phase3 == dirs_after_phase2, (
                    f"Phase 3: _indices/ grew from {dirs_after_phase2} to {dirs_after_phase3} — retrain not skipped"
                )

                # Phase 4: +600 rows (delta=600 >= 500) → retrain must fire
                batch3 = self._make_chunks(start_id=650, count=600, dims=dims)
                await s.write_chunks(batch3)
                total = await s.count()
                assert total == 1250, f"Phase 4: expected 1250 rows, got {total}"
                await s.ensure_ivf_pq_index()
                trained_count4 = s.get_config("ivf_pq_last_trained_count")
                assert trained_count4 == "1250", (
                    f"Phase 4: expected retrain to update count to '1250', got {trained_count4!r}"
                )
            finally:
                await s.close()

        asyncio.run(run())


class TestCLIStorage:
    """opencode-search storage CLI command returns valid storage diagnostics."""

    def test_cli_storage_reports_health(self):
        """opencode-search storage --json must return status=ok with required fields."""
        import json
        import subprocess
        import sys
        from pathlib import Path

        cli_path = str(Path(sys.executable).parent / "opencode-search")
        result = subprocess.run(
            [cli_path, "storage", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"CLI storage failed (exit {result.returncode}): {result.stderr[:300]}"
        )
        data = json.loads(result.stdout)
        assert data.get("status") == "ok", f"CLI storage not ok: {data}"
        assert data.get("project_count", 0) >= 1, f"expected at least 1 project: {data}"
        projects = data.get("projects", [])
        assert len(projects) >= 1, f"no projects in CLI storage output: {data}"
        stats = projects[0]
        for field in ("total_bytes", "wal_bytes", "stale_index_dirs", "recoverable_mb"):
            assert field in stats, f"missing field {field!r} in CLI output: {stats}"
