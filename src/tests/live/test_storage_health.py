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

    @pytest.mark.slow
    def test_stale_index_dirs_bounded_after_vacuum(self, http, astro):
        """After Storage.vacuum() runs, stale_index_dirs must not increase."""
        import asyncio

        from opencode_search.config import get_project_index_dir, load_registry
        from opencode_search.storage import Storage

        registry = load_registry()
        entry = registry.get(astro)
        assert entry is not None, "redacted-name-3 not in registry"

        db_path = str(get_project_index_dir(astro) / "index")
        dims = int(getattr(entry, "dims", 768) or 768)

        r_before = http.get("/api/storage_health", params={"project": astro})
        stats_before = r_before.json()["projects"][0]
        stale_before = stats_before["stale_index_dirs"]

        async def _run_vacuum():
            s = Storage(db_path=db_path, dims=dims)
            await s.open()
            try:
                return await s.vacuum()
            finally:
                await s.close()

        result = asyncio.run(_run_vacuum())
        assert result.get("status") == "ok", f"vacuum failed: {result}"

        r_after = http.get("/api/storage_health", params={"project": astro})
        stats_after = r_after.json()["projects"][0]
        assert stats_after["stale_index_dirs"] <= stale_before, (
            f"stale_index_dirs increased after vacuum: {stale_before} → "
            f"{stats_after['stale_index_dirs']}"
        )


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
    """Storage.vacuum() reclaims stale _indices/ dirs when called."""

    @pytest.mark.slow
    def test_vacuum_reclaims_stale_index_dirs(self, http, astro):
        """Calling Storage.vacuum() on an indexed project reduces stale index dirs."""
        import asyncio
        import pathlib

        from opencode_search.config import get_project_index_dir, load_registry
        from opencode_search.storage import Storage

        registry = load_registry()
        entry = registry.get(astro)
        assert entry is not None, "redacted-name-3 not in registry"

        db_path = str(get_project_index_dir(astro) / "index")
        dims = int(getattr(entry, "dims", 768) or 768)

        indices_dir = pathlib.Path(db_path) / "chunks.lance" / "_indices"
        dirs_before = sum(1 for e in os.scandir(str(indices_dir)) if e.is_dir()) if indices_dir.exists() else 0

        async def _run_vacuum():
            s = Storage(db_path=db_path, dims=dims)
            await s.open()
            try:
                return await s.vacuum()
            finally:
                await s.close()

        result = asyncio.run(_run_vacuum())
        assert result.get("status") == "ok", f"vacuum failed: {result}"

        dirs_after = sum(1 for e in os.scandir(str(indices_dir)) if e.is_dir()) if indices_dir.exists() else 0

        if dirs_before > 2:
            assert dirs_after < dirs_before, (
                f"vacuum did not reduce _indices dirs: before={dirs_before}, after={dirs_after}"
            )
        assert dirs_after <= dirs_before, (
            f"vacuum increased _indices dirs: {dirs_before} → {dirs_after}"
        )


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

    @pytest.mark.slow
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
