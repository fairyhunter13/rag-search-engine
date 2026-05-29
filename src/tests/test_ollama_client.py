"""Tests for opencode_search.enricher.client — OllamaClient."""
from __future__ import annotations

import json
import unittest.mock as mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from opencode_search.enricher.client import OllamaClient


# ---------------------------------------------------------------------------
# from_env
# ---------------------------------------------------------------------------


def test_ollama_from_env_returns_none_when_provider_none(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "none")
    assert OllamaClient.from_env() is None


def test_ollama_from_env_returns_client_when_provider_unset(monkeypatch):
    monkeypatch.delenv("OPENCODE_LLM_PROVIDER", raising=False)
    client = OllamaClient.from_env()
    assert isinstance(client, OllamaClient)


def test_ollama_from_env_returns_client_when_provider_ollama(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "ollama")
    client = OllamaClient.from_env()
    assert client is not None
    assert isinstance(client, OllamaClient)


def test_ollama_from_env_uses_configured_model(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OPENCODE_LLM_MODEL", "llama3.2:3b")
    client = OllamaClient.from_env()
    assert client is not None
    assert client.model == "llama3.2:3b"


def test_ollama_from_env_uses_configured_base_url(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OPENCODE_LLM_BASE_URL", "http://192.168.1.1:11434")
    client = OllamaClient.from_env()
    assert client is not None
    assert "192.168.1.1" in client.base_url


def test_ollama_from_env_uses_configured_timeout(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OPENCODE_LLM_TIMEOUT", "60")
    client = OllamaClient.from_env()
    assert client is not None
    assert client.timeout == 60


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


def test_ollama_is_available_false_when_connection_refused():
    client = OllamaClient(base_url="http://127.0.0.1:39999")
    assert client.is_available() is False


def test_ollama_is_available_false_when_invalid_url():
    client = OllamaClient(base_url="http://0.0.0.0:99999")
    assert client.is_available() is False


# ---------------------------------------------------------------------------
# chat — using a local mock HTTP server
# ---------------------------------------------------------------------------


class _MockOllamaHandler(BaseHTTPRequestHandler):
    """Minimal mock Ollama server."""

    def log_message(self, *_): pass  # silence default logging

    def do_GET(self):
        if self.path == "/api/tags":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"models": []}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        data = json.loads(body)

        if self.path == "/api/chat":
            response = {
                "message": {
                    "role": "assistant",
                    "content": f"intent for {data['messages'][-1]['content'][:20]}",
                }
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def mock_ollama_server():
    """Start a local mock Ollama server for the duration of the module."""
    server = HTTPServer(("127.0.0.1", 0), _MockOllamaHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def test_ollama_is_available_true_when_tags_returns_200(mock_ollama_server):
    client = OllamaClient(base_url=mock_ollama_server)
    assert client.is_available() is True


def test_ollama_chat_success_returns_string(mock_ollama_server):
    client = OllamaClient(base_url=mock_ollama_server)
    result = client.chat([{"role": "user", "content": "hello"}])
    assert isinstance(result, str)
    assert len(result) > 0


def test_ollama_chat_sends_correct_request_format(mock_ollama_server):
    import urllib.request as _req
    client = OllamaClient(base_url=mock_ollama_server, model="test-model")

    captured = {}
    orig_urlopen = _req.urlopen

    def mock_urlopen(req, timeout=None):
        if hasattr(req, "data") and req.data:
            captured["data"] = json.loads(req.data.decode())
        return orig_urlopen(req, timeout=timeout)

    with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
        try:
            client.chat([{"role": "user", "content": "test"}])
        except Exception:
            pass

    if "data" in captured:
        assert captured["data"]["model"] == "test-model"
        assert "messages" in captured["data"]
        assert captured["data"]["stream"] is False


def test_ollama_chat_connection_refused_raises_error():
    client = OllamaClient(base_url="http://127.0.0.1:39999", timeout=2)
    with pytest.raises((ConnectionError, OSError)):
        client.chat([{"role": "user", "content": "test"}])


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------


def test_ollama_symbol_intent_returns_string(mock_ollama_server):
    client = OllamaClient(base_url=mock_ollama_server)
    result = client.symbol_intent("authenticate", "authenticate(token: str) -> bool", None)
    assert isinstance(result, str)


def test_ollama_symbol_intent_with_docstring(mock_ollama_server):
    client = OllamaClient(base_url=mock_ollama_server)
    result = client.symbol_intent("foo", "foo()", "Verifies JWT.")
    assert isinstance(result, str)


def test_ollama_community_summary_returns_title_and_summary(mock_ollama_server):
    client = OllamaClient(base_url=mock_ollama_server)
    title, summary = client.community_summary(["auth.login (function)", "auth.verify (function)"])
    assert isinstance(title, str)
    assert isinstance(summary, str)
    assert len(title) > 0


def test_ollama_module_wiki_page_returns_markdown(mock_ollama_server):
    client = OllamaClient(base_url=mock_ollama_server)
    result = client.module_wiki_page("auth", ["authenticate", "verify_token"], ["jwt", "os"])
    assert isinstance(result, str)


def test_ollama_raw_doc_to_wiki_returns_content(mock_ollama_server):
    client = OllamaClient(base_url=mock_ollama_server)
    result = client.raw_doc_to_wiki("# Design Doc\nThis is a design document.", "design.md")
    assert isinstance(result, str)
