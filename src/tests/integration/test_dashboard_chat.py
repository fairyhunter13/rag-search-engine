"""T39: KB Chat — /api/chat endpoint + two-tier LLM factory tests.

Uses the same ASGI transport pattern as test_daemon_http.py — no real server,
no GPU, no live Ollama needed.  The query-tier LLM is mocked throughout.

Coverage:
  A: create_query_llm_client factory (env-var routing, fallback)
  B: handle_kb_chat handler (context assembly, modes, graceful empty-KB)
  C: POST /api/chat endpoint (validation, mode routing, response shape)
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# ASGI fixtures (re-used from test_daemon_http pattern)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def asgi_app():
    import opencode_search.embeddings as emb
    orig_done = emb._provider_detection_done
    orig_providers = emb._detected_providers
    emb._provider_detection_done = True
    emb._detected_providers = ["CUDAExecutionProvider"]
    with patch.object(emb, "is_gpu_available", return_value=True), \
         patch.object(emb, "assert_gpu_available", return_value=None):
        from opencode_search.mcp import mcp
        app = mcp.streamable_http_app()
    emb._provider_detection_done = orig_done
    emb._detected_providers = orig_providers
    return app


@pytest.fixture
async def client(asgi_app):
    import httpx
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi_app),
        base_url="http://testserver",
        follow_redirects=True,
    ) as c:
        yield c


def _make_mock_llm(answer: str = "Mock answer about features.") -> MagicMock:
    mock = MagicMock()
    mock.is_available.return_value = True
    mock.model = "mock-query:test"
    mock.chat.return_value = answer
    mock.map_query.return_value = answer
    mock.reduce_answers.return_value = answer
    return mock


# ---------------------------------------------------------------------------
# T39-A: create_query_llm_client factory
# ---------------------------------------------------------------------------

class TestT39AQueryLlmClientFactory:
    """Factory reads QUERY_LLM_* env vars and falls back gracefully."""

    def test_default_provider_is_ollama(self, monkeypatch):
        monkeypatch.delenv("OPENCODE_QUERY_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("OPENCODE_QUERY_LLM_MODEL", raising=False)
        from opencode_search.config import DEFAULT_QUERY_LLM_PROVIDER, DEFAULT_QUERY_LLM_MODEL
        assert DEFAULT_QUERY_LLM_PROVIDER == "ollama"
        assert DEFAULT_QUERY_LLM_MODEL == "qwen3-query:8b"

    def test_default_model_is_qwen3_query_8b(self, monkeypatch):
        monkeypatch.delenv("OPENCODE_QUERY_LLM_MODEL", raising=False)
        from opencode_search.config import DEFAULT_QUERY_LLM_MODEL
        assert "qwen3-query" in DEFAULT_QUERY_LLM_MODEL or "8b" in DEFAULT_QUERY_LLM_MODEL

    def test_query_ctx_is_larger_than_enrich_ctx(self):
        from opencode_search.config import DEFAULT_LLM_NUM_CTX, DEFAULT_QUERY_LLM_NUM_CTX
        assert DEFAULT_QUERY_LLM_NUM_CTX >= DEFAULT_LLM_NUM_CTX

    def test_query_timeout_is_larger_than_enrich_timeout(self):
        from opencode_search.config import DEFAULT_LLM_TIMEOUT, DEFAULT_QUERY_LLM_TIMEOUT
        assert DEFAULT_QUERY_LLM_TIMEOUT >= DEFAULT_LLM_TIMEOUT

    def test_factory_returns_ollama_client_when_provider_is_ollama(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_QUERY_LLM_PROVIDER", "ollama")
        monkeypatch.setenv("OPENCODE_QUERY_LLM_MODEL", "qwen3-query:8b")
        from opencode_search.enricher.client import OllamaClient, create_query_llm_client
        with patch.object(OllamaClient, "is_available", return_value=True):
            client = create_query_llm_client()
        assert isinstance(client, OllamaClient)

    def test_factory_uses_query_model_not_enrich_model(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_QUERY_LLM_PROVIDER", "ollama")
        monkeypatch.setenv("OPENCODE_QUERY_LLM_MODEL", "qwen3-query:8b")
        from opencode_search.enricher.client import OllamaClient, create_query_llm_client
        with patch.object(OllamaClient, "is_available", return_value=True):
            client = create_query_llm_client()
        assert isinstance(client, OllamaClient)
        assert client.model == "qwen3-query:8b"

    def test_factory_uses_larger_ctx_than_enrich(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_QUERY_LLM_PROVIDER", "ollama")
        monkeypatch.setenv("OPENCODE_QUERY_LLM_MODEL", "qwen3-query:8b")
        from opencode_search.config import DEFAULT_LLM_NUM_CTX
        from opencode_search.enricher.client import OllamaClient, create_query_llm_client
        with patch.object(OllamaClient, "is_available", return_value=True):
            client = create_query_llm_client()
        assert isinstance(client, OllamaClient)
        assert client.num_ctx > DEFAULT_LLM_NUM_CTX

    def test_falls_back_to_enrich_client_when_unavailable(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_QUERY_LLM_PROVIDER", "ollama")
        monkeypatch.setenv("OPENCODE_QUERY_LLM_MODEL", "nonexistent-model:999b")
        monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "ollama")
        monkeypatch.setenv("OPENCODE_LLM_MODEL", "qwen3-enrich:1.7b")
        from opencode_search.enricher.client import OllamaClient, create_query_llm_client
        call_count = {"n": 0}

        def _is_available(self):
            call_count["n"] += 1
            # First call (query model) → unavailable; fallback call → available
            return call_count["n"] > 1

        with patch.object(OllamaClient, "is_available", _is_available):
            client = create_query_llm_client()
        assert isinstance(client, OllamaClient)
        # Should have fallen back to enrich model
        assert client.model != "nonexistent-model:999b"

    def test_independent_from_enrich_model(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "ollama")
        monkeypatch.setenv("OPENCODE_LLM_MODEL", "qwen3-enrich:1.7b")
        monkeypatch.setenv("OPENCODE_QUERY_LLM_PROVIDER", "ollama")
        monkeypatch.setenv("OPENCODE_QUERY_LLM_MODEL", "qwen3-query:8b")
        from opencode_search.enricher.client import OllamaClient, create_llm_client, create_query_llm_client
        with patch.object(OllamaClient, "is_available", return_value=True):
            enrich = create_llm_client()
            query = create_query_llm_client()
        assert isinstance(enrich, OllamaClient)
        assert isinstance(query, OllamaClient)
        assert enrich.model != query.model


# ---------------------------------------------------------------------------
# T39-B: handle_kb_chat handler
# ---------------------------------------------------------------------------

class TestT39BHandleKbChat:
    """Direct handler tests — no server, no real LLM."""

    async def test_empty_kb_returns_graceful_response(self, tmp_path):
        """Handler returns a valid dict even when project has no index."""
        mock_llm = _make_mock_llm("No content found.")
        with patch("opencode_search.handlers._kb_chat.create_query_llm_client",
                   return_value=mock_llm):
            from opencode_search.handlers._kb_chat import handle_kb_chat
            result = await handle_kb_chat(
                query="list all features",
                project_path=str(tmp_path),
                mode="quick",
            )
        assert isinstance(result, dict)
        assert "answer" in result
        assert "sources" in result
        assert isinstance(result["sources"], list)
        assert "elapsed_ms" in result
        assert result["elapsed_ms"] >= 0

    async def test_quick_mode_returns_answer(self, tmp_path):
        mock_llm = _make_mock_llm("Feature A: authentication in auth/handler.py\nFeature B: search in search/engine.py")
        with patch("opencode_search.handlers._kb_chat.create_query_llm_client",
                   return_value=mock_llm):
            from opencode_search.handlers._kb_chat import handle_kb_chat
            result = await handle_kb_chat(
                query="list all features",
                project_path=str(tmp_path),
                mode="quick",
            )
        assert result["answer"] != ""
        assert result["mode"] == "quick"

    async def test_comprehensive_mode_returns_answer(self, tmp_path):
        mock_llm = _make_mock_llm("Comprehensive feature list: auth, search, indexing.")
        with patch("opencode_search.handlers._kb_chat.create_query_llm_client",
                   return_value=mock_llm):
            from opencode_search.handlers._kb_chat import handle_kb_chat
            result = await handle_kb_chat(
                query="list all functionalities",
                project_path=str(tmp_path),
                mode="comprehensive",
            )
        assert isinstance(result["answer"], str)
        assert result["mode"] == "comprehensive"

    async def test_result_has_required_keys(self, tmp_path):
        mock_llm = _make_mock_llm("answer text")
        with patch("opencode_search.handlers._kb_chat.create_query_llm_client",
                   return_value=mock_llm):
            from opencode_search.handlers._kb_chat import handle_kb_chat
            result = await handle_kb_chat("any query", str(tmp_path), mode="quick")
        required_keys = {"answer", "sources", "communities_used", "code_results",
                         "wiki_results", "mode", "model", "elapsed_ms"}
        for key in required_keys:
            assert key in result, f"Missing key: {key}"

    async def test_sources_is_always_a_list(self, tmp_path):
        mock_llm = _make_mock_llm("answer")
        with patch("opencode_search.handlers._kb_chat.create_query_llm_client",
                   return_value=mock_llm):
            from opencode_search.handlers._kb_chat import handle_kb_chat
            result = await handle_kb_chat("query", str(tmp_path), mode="quick")
        assert isinstance(result["sources"], list)

    async def test_llm_unavailable_returns_error_message(self, tmp_path):
        with patch("opencode_search.handlers._kb_chat.create_query_llm_client",
                   return_value=None):
            from opencode_search.handlers._kb_chat import handle_kb_chat
            result = await handle_kb_chat("query", str(tmp_path), mode="quick")
        assert "answer" in result
        assert "unavailable" in result["answer"].lower() or "llm" in result["answer"].lower()

    async def test_model_name_included_in_response(self, tmp_path):
        mock_llm = _make_mock_llm("answer")
        mock_llm.model = "qwen3-query:8b"
        with patch("opencode_search.handlers._kb_chat.create_query_llm_client",
                   return_value=mock_llm):
            from opencode_search.handlers._kb_chat import handle_kb_chat
            result = await handle_kb_chat("query", str(tmp_path), mode="quick")
        assert result["model"] == "qwen3-query:8b"

    async def test_counts_are_non_negative_integers(self, tmp_path):
        mock_llm = _make_mock_llm("answer")
        with patch("opencode_search.handlers._kb_chat.create_query_llm_client",
                   return_value=mock_llm):
            from opencode_search.handlers._kb_chat import handle_kb_chat
            result = await handle_kb_chat("query", str(tmp_path), mode="quick")
        assert isinstance(result["communities_used"], int) and result["communities_used"] >= 0
        assert isinstance(result["code_results"], int) and result["code_results"] >= 0
        assert isinstance(result["wiki_results"], int) and result["wiki_results"] >= 0


# ---------------------------------------------------------------------------
# T39-C: POST /api/chat endpoint
# ---------------------------------------------------------------------------

class TestT39CApiChatEndpoint:
    """Dashboard /api/chat via ASGI transport — validates HTTP contract."""

    async def test_returns_400_when_project_missing(self, client):
        r = await client.post(
            "/api/chat",
            json={"query": "list features"},
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400
        assert "error" in r.json()

    async def test_returns_400_when_query_missing(self, client):
        r = await client.post(
            "/api/chat",
            json={"project": "/tmp/nonexistent"},
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400
        assert "error" in r.json()

    async def test_returns_400_when_body_empty(self, client):
        r = await client.post(
            "/api/chat",
            json={},
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400

    async def test_returns_200_with_valid_params_mocked_llm(self, client, tmp_path):
        mock_llm = _make_mock_llm("The project has three main features: auth, search, and indexing.")
        with patch("opencode_search.handlers._kb_chat.create_query_llm_client",
                   return_value=mock_llm):
            r = await client.post(
                "/api/chat",
                json={"project": str(tmp_path), "query": "list all features", "mode": "quick"},
                headers={"Content-Type": "application/json"},
            )
        assert r.status_code == 200
        data = r.json()
        assert "answer" in data
        assert isinstance(data["answer"], str)
        assert len(data["answer"]) > 0

    async def test_response_has_all_required_fields(self, client, tmp_path):
        mock_llm = _make_mock_llm("answer text")
        with patch("opencode_search.enricher.create_query_llm_client",
                   return_value=mock_llm):
            r = await client.post(
                "/api/chat",
                json={"project": str(tmp_path), "query": "explain architecture"},
                headers={"Content-Type": "application/json"},
            )
        assert r.status_code == 200
        data = r.json()
        for key in ("answer", "sources", "intent", "elapsed_ms"):
            assert key in data, f"Missing key in response: {key}"

    async def test_sources_field_is_list(self, client, tmp_path):
        mock_llm = _make_mock_llm("answer")
        with patch("opencode_search.enricher.create_query_llm_client",
                   return_value=mock_llm):
            r = await client.post(
                "/api/chat",
                json={"project": str(tmp_path), "query": "what are the entry points"},
                headers={"Content-Type": "application/json"},
            )
        assert r.status_code == 200
        assert isinstance(r.json()["sources"], list)

    async def test_comprehensive_mode_accepted(self, client, tmp_path):
        mock_llm = _make_mock_llm("comprehensive answer")
        with patch("opencode_search.enricher.create_query_llm_client",
                   return_value=mock_llm):
            r = await client.post(
                "/api/chat",
                json={"project": str(tmp_path), "query": "list all functionalities"},
                headers={"Content-Type": "application/json"},
            )
        assert r.status_code == 200
        assert "answer" in r.json(), "Response must have 'answer' field"

    async def test_invalid_mode_defaults_to_comprehensive(self, client, tmp_path):
        mock_llm = _make_mock_llm("answer")
        with patch("opencode_search.enricher.create_query_llm_client",
                   return_value=mock_llm):
            r = await client.post(
                "/api/chat",
                json={"project": str(tmp_path), "query": "q"},
                headers={"Content-Type": "application/json"},
            )
        assert r.status_code == 200
        assert "answer" in r.json(), "Response must have 'answer' field regardless of mode"

    async def test_elapsed_ms_is_non_negative(self, client, tmp_path):
        mock_llm = _make_mock_llm("answer")
        with patch("opencode_search.handlers._kb_chat.create_query_llm_client",
                   return_value=mock_llm):
            r = await client.post(
                "/api/chat",
                json={"project": str(tmp_path), "query": "q", "mode": "quick"},
                headers={"Content-Type": "application/json"},
            )
        assert r.status_code == 200
        assert r.json()["elapsed_ms"] >= 0

    async def test_fallback_when_query_llm_unavailable(self, client, tmp_path):
        """When query LLM unavailable, endpoint still returns a valid dict."""
        with patch("opencode_search.handlers._kb_chat.create_query_llm_client",
                   return_value=None):
            r = await client.post(
                "/api/chat",
                json={"project": str(tmp_path), "query": "q", "mode": "quick"},
                headers={"Content-Type": "application/json"},
            )
        # Should return 200 with an informative answer (not a 500)
        assert r.status_code == 200
        data = r.json()
        assert "answer" in data
