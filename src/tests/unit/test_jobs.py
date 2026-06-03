"""Unit tests for opencode_search.jobs — background job registry."""
from __future__ import annotations

import asyncio

import pytest

from opencode_search.jobs import (
    Job,
    cancel_job,
    get_job,
    job_to_dict,
    list_jobs,
    submit_job,
)


async def _dummy_ok() -> dict:
    return {"status": "ok", "data": "done"}


async def _dummy_error() -> dict:
    raise ValueError("intentional failure")


async def _dummy_slow() -> dict:
    await asyncio.sleep(60)
    return {"status": "ok"}


class TestSubmitJob:
    async def test_returns_job_immediately(self):
        job = submit_job(_dummy_ok(), action="pipeline", project_path="/tmp/p")
        assert isinstance(job, Job)
        assert job.id
        assert job.status in ("queued", "running")

    async def test_job_completes_ok(self):
        job = submit_job(_dummy_ok(), action="pipeline", project_path="/tmp/p")
        await asyncio.sleep(0.1)
        assert job.status == "ok"
        assert job.result == {"status": "ok", "data": "done"}
        assert job.completed_at is not None

    async def test_job_error_captured(self):
        job = submit_job(_dummy_error(), action="enrich", project_path="/tmp/p")
        await asyncio.sleep(0.1)
        assert job.status == "error"
        assert "intentional failure" in (job.error or "")

    async def test_get_job_returns_same_instance(self):
        job = submit_job(_dummy_ok(), action="wiki", project_path="/tmp/p")
        found = get_job(job.id)
        assert found is job

    async def test_get_job_unknown_returns_none(self):
        assert get_job("nonexistent-id") is None

    async def test_list_jobs_includes_submitted(self):
        job = submit_job(_dummy_ok(), action="pipeline", project_path="/tmp/list-test")
        jobs = list_jobs()
        assert any(j.id == job.id for j in jobs)

    async def test_list_jobs_filter_by_project(self):
        p = "/tmp/filter-project"
        job = submit_job(_dummy_ok(), action="pipeline", project_path=p)
        await asyncio.sleep(0.05)
        filtered = list_jobs(project_path=p)
        assert all(j.project_path == p for j in filtered)
        assert any(j.id == job.id for j in filtered)

    async def test_list_jobs_filter_by_action(self):
        job = submit_job(_dummy_ok(), action="hierarchy", project_path="/tmp/p")
        await asyncio.sleep(0.05)
        filtered = list_jobs(action="hierarchy")
        assert all(j.action == "hierarchy" for j in filtered)
        assert any(j.id == job.id for j in filtered)

    async def test_job_to_dict_has_required_keys(self):
        job = submit_job(_dummy_ok(), action="pipeline", project_path="/tmp/p")
        await asyncio.sleep(0.1)
        d = job_to_dict(job)
        for key in ("id", "action", "project_path", "status", "queued_at", "started_at", "completed_at", "error", "result"):
            assert key in d

    async def test_cancel_running_job(self):
        job = submit_job(_dummy_slow(), action="pipeline", project_path="/tmp/p")
        await asyncio.sleep(0.05)
        assert job.status == "running"
        cancel_job(job.id)
        await asyncio.sleep(0.1)
        assert job.status == "cancelled"

    async def test_cancel_unknown_job_returns_false(self):
        assert cancel_job("no-such-job") is False
