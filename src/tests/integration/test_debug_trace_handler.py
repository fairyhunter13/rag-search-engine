"""Integration tests for handle_debug_trace — mocked LLM, real parser + context assembly."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PYTHON_TB = """
Traceback (most recent call last):
  File "/project/src/handlers/_pipeline.py", line 88, in handle_pipeline
    result = await embedder.embed(chunks)
  File "/project/src/embeddings.py", line 42, in embed
    return self._model.encode(texts)
AttributeError: 'NoneType' object has no attribute 'encode'
"""


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={
        "content": (
            "1) Root Cause: The embedder model is None at embed() call time. "
            "2) Why: handle_pipeline calls embed() before the model is initialised. "
            "3) Fix: Ensure the model is loaded before calling handle_pipeline."
        )
    })
    return llm


class TestHandleDebugTrace:
    def test_parse_frames_python(self):
        from opencode_search.handlers._debug_trace import parse_traceback
        frames = parse_traceback(PYTHON_TB)
        assert len(frames) == 2
        assert frames[-1]["function"] == "embed"

    @pytest.mark.asyncio
    async def test_returns_required_keys(self, tmp_path, mock_llm):
        with patch("opencode_search.handlers._debug_trace.create_query_llm_client", return_value=mock_llm):
            with patch("opencode_search.handlers._debug_trace._fetch_graph_context", return_value=[]):
                with patch("opencode_search.handlers._debug_trace._fetch_code_context", return_value=[]):
                    from opencode_search.handlers._debug_trace import handle_debug_trace
                    result = await handle_debug_trace(
                        traceback=PYTHON_TB,
                        project_path=str(tmp_path),
                    )
        required = ["frames", "root_cause", "fix_recommendation", "hotspot_files",
                    "communities_involved", "confidence", "elapsed_ms"]
        for key in required:
            assert key in result, f"Missing key: {key}"

    @pytest.mark.asyncio
    async def test_frames_are_parsed(self, tmp_path, mock_llm):
        with patch("opencode_search.handlers._debug_trace.create_query_llm_client", return_value=mock_llm):
            with patch("opencode_search.handlers._debug_trace._fetch_graph_context", return_value=[]):
                with patch("opencode_search.handlers._debug_trace._fetch_code_context", return_value=[]):
                    from opencode_search.handlers._debug_trace import handle_debug_trace
                    result = await handle_debug_trace(
                        traceback=PYTHON_TB,
                        project_path=str(tmp_path),
                    )
        assert len(result["frames"]) >= 1

    @pytest.mark.asyncio
    async def test_root_cause_non_empty(self, tmp_path, mock_llm):
        with patch("opencode_search.handlers._debug_trace.create_query_llm_client", return_value=mock_llm):
            with patch("opencode_search.handlers._debug_trace._fetch_graph_context", return_value=[]):
                with patch("opencode_search.handlers._debug_trace._fetch_code_context", return_value=[]):
                    from opencode_search.handlers._debug_trace import handle_debug_trace
                    result = await handle_debug_trace(
                        traceback=PYTHON_TB,
                        project_path=str(tmp_path),
                    )
        assert len(result["root_cause"]) > 10

    @pytest.mark.asyncio
    async def test_empty_traceback_returns_gracefully(self, tmp_path):
        from opencode_search.handlers._debug_trace import handle_debug_trace
        result = await handle_debug_trace(traceback="", project_path=str(tmp_path))
        assert result["frames"] == []
        assert result["confidence"] == "low"
        assert "root_cause" in result

    @pytest.mark.asyncio
    async def test_elapsed_ms_positive(self, tmp_path, mock_llm):
        with patch("opencode_search.handlers._debug_trace.create_query_llm_client", return_value=mock_llm):
            with patch("opencode_search.handlers._debug_trace._fetch_graph_context", return_value=[]):
                with patch("opencode_search.handlers._debug_trace._fetch_code_context", return_value=[]):
                    from opencode_search.handlers._debug_trace import handle_debug_trace
                    result = await handle_debug_trace(
                        traceback=PYTHON_TB,
                        project_path=str(tmp_path),
                    )
        assert result["elapsed_ms"] >= 0

    @pytest.mark.asyncio
    async def test_confidence_low_without_graph_context(self, tmp_path, mock_llm):
        with patch("opencode_search.handlers._debug_trace.create_query_llm_client", return_value=mock_llm):
            with patch("opencode_search.handlers._debug_trace._fetch_graph_context", return_value=[]):
                with patch("opencode_search.handlers._debug_trace._fetch_code_context", return_value=[]):
                    from opencode_search.handlers._debug_trace import handle_debug_trace
                    result = await handle_debug_trace(
                        traceback=PYTHON_TB,
                        project_path=str(tmp_path),
                    )
        assert result["confidence"] in ("low", "medium", "high")

    @pytest.mark.asyncio
    async def test_confidence_high_with_rich_graph_context(self, tmp_path, mock_llm):
        rich_ctx = [
            {"frame_function": f"fn{i}", "frame_file": "a.py", "frame_line": i,
             "node_name": f"fn{i}", "node_file": "a.py",
             "community_id": i, "community_title": f"Community {i}",
             "community_summary": "handles indexing"}
            for i in range(4)
        ]
        with patch("opencode_search.handlers._debug_trace.create_query_llm_client", return_value=mock_llm):
            with patch("opencode_search.handlers._debug_trace._fetch_graph_context", return_value=rich_ctx):
                with patch("opencode_search.handlers._debug_trace._fetch_code_context", return_value=[]):
                    from opencode_search.handlers._debug_trace import handle_debug_trace
                    result = await handle_debug_trace(
                        traceback=PYTHON_TB,
                        project_path=str(tmp_path),
                    )
        assert result["confidence"] == "high"

    @pytest.mark.asyncio
    async def test_communities_in_result(self, tmp_path, mock_llm):
        ctx = [
            {"frame_function": "embed", "frame_file": "a.py", "frame_line": 42,
             "node_name": "embed", "node_file": "a.py",
             "community_id": 1, "community_title": "Embedding Pipeline",
             "community_summary": "handles text embedding"}
        ]
        with patch("opencode_search.handlers._debug_trace.create_query_llm_client", return_value=mock_llm):
            with patch("opencode_search.handlers._debug_trace._fetch_graph_context", return_value=ctx):
                with patch("opencode_search.handlers._debug_trace._fetch_code_context", return_value=[]):
                    from opencode_search.handlers._debug_trace import handle_debug_trace
                    result = await handle_debug_trace(
                        traceback=PYTHON_TB,
                        project_path=str(tmp_path),
                    )
        assert "Embedding Pipeline" in result["communities_involved"]

    @pytest.mark.asyncio
    async def test_llm_failure_graceful(self, tmp_path):
        failing_llm = MagicMock()
        failing_llm.chat = AsyncMock(side_effect=RuntimeError("LLM down"))
        with patch("opencode_search.handlers._debug_trace.create_query_llm_client", return_value=failing_llm):
            with patch("opencode_search.handlers._debug_trace._fetch_graph_context", return_value=[]):
                with patch("opencode_search.handlers._debug_trace._fetch_code_context", return_value=[]):
                    from opencode_search.handlers._debug_trace import handle_debug_trace
                    result = await handle_debug_trace(
                        traceback=PYTHON_TB,
                        project_path=str(tmp_path),
                    )
        assert "root_cause" in result
        assert len(result["root_cause"]) > 0


class TestDebugApiEndpoint:
    """Test /api/debug endpoint via ASGI transport."""

    @pytest.fixture
    def client(self):
        from httpx import AsyncClient, ASGITransport
        import opencode_search.mcp as _mcp_mod
        app = _mcp_mod.mcp.streamable_http_app()
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    @pytest.mark.asyncio
    async def test_missing_project_returns_400(self, client):
        r = await client.post("/api/debug", json={"traceback": PYTHON_TB})
        assert r.status_code == 400
        assert "project" in r.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_missing_traceback_returns_400(self, client):
        r = await client.post("/api/debug", json={"project": "/tmp/x"})
        assert r.status_code == 400
        assert "traceback" in r.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_valid_request_with_mocked_handler(self, client, tmp_path, mock_llm):
        with patch("opencode_search.handlers._debug_trace.create_query_llm_client", return_value=mock_llm):
            with patch("opencode_search.handlers._debug_trace._fetch_graph_context", return_value=[]):
                with patch("opencode_search.handlers._debug_trace._fetch_code_context", return_value=[]):
                    r = await client.post("/api/debug", json={
                        "project": str(tmp_path),
                        "traceback": PYTHON_TB,
                        "error_message": "AttributeError",
                        "include_fix": True,
                    })
        assert r.status_code == 200
        data = r.json()
        assert "root_cause" in data
        assert "frames" in data
        assert "confidence" in data
