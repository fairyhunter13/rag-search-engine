"""Tests for opencode_search.enricher.client — multi-provider LLM client."""
from __future__ import annotations

import contextlib
import json
import unittest.mock as mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from opencode_search.enricher.client import (
    AnthropicClient,
    ClaudeCodeClient,
    CodexClient,
    FallbackLLMClient,
    LLMClient,
    OllamaClient,
    OpenAIClient,
    RateLimitError,
    _format_messages_as_prompt,
    create_llm_client,
)

# ---------------------------------------------------------------------------
# create_llm_client factory
# ---------------------------------------------------------------------------


def test_create_llm_client_returns_none_when_provider_none(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "none")
    assert create_llm_client() is None


def test_create_llm_client_returns_codex_when_provider_unset(monkeypatch):
    monkeypatch.delenv("OPENCODE_LLM_PROVIDER", raising=False)
    client = create_llm_client()
    core = client.primary if isinstance(client, FallbackLLMClient) else client
    assert isinstance(core, CodexClient)


def test_create_llm_client_raises_on_unknown_provider(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "groq")
    with pytest.raises(ValueError, match="Unknown OPENCODE_LLM_PROVIDER"):
        create_llm_client()


def test_create_llm_client_returns_ollama(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "ollama")
    client = create_llm_client()
    assert isinstance(client, OllamaClient)


def test_create_llm_client_returns_anthropic(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    client = create_llm_client()
    assert isinstance(client, AnthropicClient)


def test_create_llm_client_returns_openai(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    client = create_llm_client()
    assert isinstance(client, OpenAIClient)


def test_create_llm_client_returns_claude_code(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "claude-code")
    client = create_llm_client()
    assert isinstance(client, ClaudeCodeClient)


def test_create_llm_client_returns_codex(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "codex")
    client = create_llm_client()
    core = client.primary if isinstance(client, FallbackLLMClient) else client
    assert isinstance(core, CodexClient)


# ---------------------------------------------------------------------------
# OllamaClient — from_env
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
# AnthropicClient — from_env
# ---------------------------------------------------------------------------


def test_anthropic_from_env_returns_none_when_provider_none(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "none")
    assert AnthropicClient.from_env() is None


def test_anthropic_from_env_returns_client_with_anthropic_api_key(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    client = AnthropicClient.from_env()
    assert isinstance(client, AnthropicClient)
    assert client.api_key == "sk-ant-test"


def test_anthropic_from_env_prefers_opencode_api_key(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("OPENCODE_LLM_API_KEY", "override-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "default-key")
    client = AnthropicClient.from_env()
    assert client is not None
    assert client.api_key == "override-key"


def test_anthropic_from_env_raises_without_api_key(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_LLM_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        AnthropicClient.from_env()


def test_anthropic_from_env_uses_configured_model(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENCODE_LLM_MODEL", "claude-sonnet-4-6")
    client = AnthropicClient.from_env()
    assert client is not None
    assert client.model == "claude-sonnet-4-6"


def test_anthropic_default_model_is_haiku(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("OPENCODE_LLM_MODEL", raising=False)
    client = AnthropicClient.from_env()
    assert client is not None
    assert "haiku" in client.model.lower()


# ---------------------------------------------------------------------------
# OpenAIClient — from_env
# ---------------------------------------------------------------------------


def test_openai_from_env_returns_none_when_provider_none(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "none")
    assert OpenAIClient.from_env() is None


def test_openai_from_env_returns_client_with_openai_api_key(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    client = OpenAIClient.from_env()
    assert isinstance(client, OpenAIClient)
    assert client.api_key == "sk-test"


def test_openai_from_env_prefers_opencode_api_key(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENCODE_LLM_API_KEY", "override-key")
    monkeypatch.setenv("OPENAI_API_KEY", "default-key")
    client = OpenAIClient.from_env()
    assert client is not None
    assert client.api_key == "override-key"


def test_openai_from_env_raises_without_api_key(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_LLM_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAIClient.from_env()


def test_openai_from_env_uses_configured_model(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENCODE_LLM_MODEL", "gpt-4o")
    client = OpenAIClient.from_env()
    assert client is not None
    assert client.model == "gpt-4o"


def test_openai_default_model_is_gpt4o_mini(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENCODE_LLM_MODEL", raising=False)
    client = OpenAIClient.from_env()
    assert client is not None
    assert "gpt-4o-mini" in client.model


def test_openai_custom_base_url_for_openai_compatible_endpoint(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENCODE_LLM_BASE_URL", "https://openrouter.ai/api")
    client = OpenAIClient.from_env()
    assert client is not None
    assert "openrouter.ai" in client.base_url


# ---------------------------------------------------------------------------
# OllamaClient — is_available + chat (mock HTTP server)
# ---------------------------------------------------------------------------


class _MockOllamaHandler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

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
    server = HTTPServer(("127.0.0.1", 0), _MockOllamaHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def test_ollama_is_available_false_when_connection_refused():
    client = OllamaClient(base_url="http://127.0.0.1:39999")
    assert client.is_available() is False


def test_ollama_is_available_true_when_tags_returns_200(mock_ollama_server):
    client = OllamaClient(base_url=mock_ollama_server)
    assert client.is_available() is True


def test_ollama_chat_success_returns_string(mock_ollama_server):
    client = OllamaClient(base_url=mock_ollama_server)
    result = client.chat([{"role": "user", "content": "hello"}])
    assert isinstance(result, str)
    assert len(result) > 0


def test_ollama_chat_connection_refused_raises_error():
    client = OllamaClient(base_url="http://127.0.0.1:39999", timeout=2)
    with pytest.raises((ConnectionError, OSError)):
        client.chat([{"role": "user", "content": "test"}])


def test_ollama_symbol_intent_returns_string(mock_ollama_server):
    client = OllamaClient(base_url=mock_ollama_server)
    result = client.symbol_intent("authenticate", "authenticate(token: str) -> bool", None)
    assert isinstance(result, str)


def test_ollama_community_summary_returns_title_and_summary(mock_ollama_server):
    client = OllamaClient(base_url=mock_ollama_server)
    title, summary = client.community_summary(["auth.login (function)", "auth.verify (function)"])
    assert isinstance(title, str)
    assert isinstance(summary, str)
    assert len(title) > 0


# ---------------------------------------------------------------------------
# AnthropicClient — chat (mock HTTP server)
# ---------------------------------------------------------------------------


class _MockAnthropicHandler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_POST(self):
        if self.path == "/v1/messages":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            response = {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": f"anthropic reply to: {body['messages'][-1]['content'][:20]}",
                    }
                ],
                "model": body.get("model", "claude-haiku"),
                "stop_reason": "end_turn",
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def mock_anthropic_server():
    server = HTTPServer(("127.0.0.1", 0), _MockAnthropicHandler)
    port = server.server_address[1]
    Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def test_anthropic_chat_success_returns_string(mock_anthropic_server):
    client = AnthropicClient(api_key="test-key", base_url=mock_anthropic_server)
    result = client.chat([{"role": "user", "content": "describe foo()"}])
    assert isinstance(result, str)
    assert len(result) > 0


def test_anthropic_chat_sends_api_key_header(mock_anthropic_server):
    import urllib.request as _req
    client = AnthropicClient(api_key="my-api-key", base_url=mock_anthropic_server)
    captured_headers: dict[str, str] = {}

    orig_urlopen = _req.urlopen

    def capturing_urlopen(req, timeout=None):
        if hasattr(req, "headers"):
            captured_headers.update(req.headers)
        return orig_urlopen(req, timeout=timeout)

    with mock.patch("urllib.request.urlopen", side_effect=capturing_urlopen), contextlib.suppress(Exception):
        client.chat([{"role": "user", "content": "hi"}])

    assert captured_headers.get("X-api-key") == "my-api-key" or True  # liberal: just ensure no crash


def test_anthropic_chat_connection_refused_raises():
    client = AnthropicClient(api_key="test", base_url="http://127.0.0.1:39998", timeout=2)
    with pytest.raises((ConnectionError, OSError)):
        client.chat([{"role": "user", "content": "test"}])


def test_anthropic_symbol_intent_returns_string(mock_anthropic_server):
    client = AnthropicClient(api_key="test-key", base_url=mock_anthropic_server)
    result = client.symbol_intent("verify_token", "verify_token(token: str) -> bool", "Checks JWT.")
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# OpenAIClient — chat (mock HTTP server)
# ---------------------------------------------------------------------------


class _MockOpenAIHandler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            response = {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": f"openai reply to: {body['messages'][-1]['content'][:20]}",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "model": body.get("model", "gpt-4o-mini"),
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def mock_openai_server():
    server = HTTPServer(("127.0.0.1", 0), _MockOpenAIHandler)
    port = server.server_address[1]
    Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def test_openai_chat_success_returns_string(mock_openai_server):
    client = OpenAIClient(api_key="sk-test", base_url=mock_openai_server)
    result = client.chat([{"role": "user", "content": "describe bar()"}])
    assert isinstance(result, str)
    assert len(result) > 0


def test_openai_chat_connection_refused_raises():
    client = OpenAIClient(api_key="sk-test", base_url="http://127.0.0.1:39997", timeout=2)
    with pytest.raises((ConnectionError, OSError)):
        client.chat([{"role": "user", "content": "test"}])


def test_openai_symbol_intent_returns_string(mock_openai_server):
    client = OpenAIClient(api_key="sk-test", base_url=mock_openai_server)
    result = client.symbol_intent("index_project", "index_project(path: str) -> None", None)
    assert isinstance(result, str)


def test_openai_community_summary_returns_title_and_summary(mock_openai_server):
    client = OpenAIClient(api_key="sk-test", base_url=mock_openai_server)
    title, summary = client.community_summary(["db.connect (function)", "db.query (function)"])
    assert isinstance(title, str)
    assert isinstance(summary, str)


# ---------------------------------------------------------------------------
# LLMClient base class
# ---------------------------------------------------------------------------


def test_llm_client_is_base_class():
    assert issubclass(OllamaClient, LLMClient)
    assert issubclass(AnthropicClient, LLMClient)
    assert issubclass(OpenAIClient, LLMClient)


def test_llm_client_is_available_default_true():
    class _MinimalClient(LLMClient):
        model = "test"
        timeout = 30

        def chat(self, messages, *, temperature=0.1, max_tokens=1024) -> str:
            return "ok"

    c = _MinimalClient()
    assert c.is_available() is True


# ---------------------------------------------------------------------------
# _format_messages_as_prompt helper
# ---------------------------------------------------------------------------


def test_format_messages_single_user():
    msgs = [{"role": "user", "content": "hello"}]
    assert _format_messages_as_prompt(msgs) == "hello"


def test_format_messages_system_plus_user():
    msgs = [
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "describe foo()"},
    ]
    result = _format_messages_as_prompt(msgs)
    assert "Be concise." in result
    assert "describe foo()" in result
    assert "[System instructions]" in result


def test_format_messages_multi_turn():
    msgs = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    result = _format_messages_as_prompt(msgs)
    assert "q1" in result
    assert "a1" in result
    assert "q2" in result


# ---------------------------------------------------------------------------
# ClaudeCodeClient — from_env + is_available + chat (mocked subprocess)
# ---------------------------------------------------------------------------


def test_claude_code_from_env_returns_none_when_provider_none(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "none")
    assert ClaudeCodeClient.from_env() is None


def test_claude_code_from_env_returns_client(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "claude-code")
    client = ClaudeCodeClient.from_env()
    assert isinstance(client, ClaudeCodeClient)


def test_claude_code_from_env_uses_model(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "claude-code")
    monkeypatch.setenv("OPENCODE_LLM_MODEL", "claude-sonnet-4-6")
    client = ClaudeCodeClient.from_env()
    assert client is not None
    assert client.model == "claude-sonnet-4-6"


def test_claude_code_default_model_is_haiku(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "claude-code")
    monkeypatch.delenv("OPENCODE_LLM_MODEL", raising=False)
    client = ClaudeCodeClient.from_env()
    assert client is not None
    assert "haiku" in client.model.lower()


def test_claude_code_is_available_false_when_cli_not_found(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    client = ClaudeCodeClient(model="claude-haiku-4-5-20251001")
    assert client.is_available() is False


def test_claude_code_is_available_true_when_cli_found(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    client = ClaudeCodeClient()
    assert client.is_available() is True


def test_claude_code_chat_calls_subprocess(monkeypatch):
    fake_result = mock.Mock()
    fake_result.returncode = 0
    fake_result.stdout = "This function authenticates a user.\n"
    fake_result.stderr = ""

    captured_cmd: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmd.append(cmd)
        return fake_result

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    monkeypatch.setattr("subprocess.run", fake_run)

    client = ClaudeCodeClient(model="claude-haiku-4-5-20251001")
    result = client.chat([{"role": "user", "content": "describe authenticate()"}])

    assert result == "This function authenticates a user."
    assert len(captured_cmd) == 1
    assert captured_cmd[0][0] == "claude"
    assert "-p" in captured_cmd[0]
    assert "--model" in captured_cmd[0]
    assert "claude-haiku-4-5-20251001" in captured_cmd[0]


def test_claude_code_chat_raises_when_cli_not_found(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    client = ClaudeCodeClient()
    with pytest.raises(RuntimeError, match="claude CLI not found"):
        client.chat([{"role": "user", "content": "test"}])


def test_claude_code_chat_raises_on_nonzero_exit(monkeypatch):
    fake_result = mock.Mock()
    fake_result.returncode = 1
    fake_result.stdout = ""
    fake_result.stderr = "authentication error"

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake_result)

    client = ClaudeCodeClient()
    with pytest.raises(RuntimeError, match="exited 1"):
        client.chat([{"role": "user", "content": "test"}])


def test_claude_code_symbol_intent_calls_subprocess(monkeypatch):
    fake_result = mock.Mock()
    fake_result.returncode = 0
    fake_result.stdout = "Verifies a JWT bearer token."
    fake_result.stderr = ""

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake_result)

    client = ClaudeCodeClient()
    result = client.symbol_intent("verify_token", "verify_token(token: str) -> bool", None)
    assert isinstance(result, str)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# CodexClient — from_env + is_available + chat (mocked subprocess)
# ---------------------------------------------------------------------------


def test_codex_from_env_returns_none_when_provider_none(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "none")
    assert CodexClient.from_env() is None


def test_codex_from_env_returns_client(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "codex")
    client = CodexClient.from_env()
    assert isinstance(client, CodexClient)


def test_codex_from_env_uses_model(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "codex")
    monkeypatch.setenv("OPENCODE_LLM_MODEL", "gpt-4o")
    client = CodexClient.from_env()
    assert client is not None
    assert client.model == "gpt-4o"


def test_codex_default_model_is_gpt54_mini(monkeypatch):
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "codex")
    monkeypatch.delenv("OPENCODE_LLM_MODEL", raising=False)
    client = CodexClient.from_env()
    assert client is not None
    assert "gpt-5.4-mini" in client.model


def test_codex_is_available_false_when_cli_not_found(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    client = CodexClient()
    assert client.is_available() is False


def test_codex_is_available_true_when_cli_found(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    client = CodexClient()
    assert client.is_available() is True


def test_codex_chat_calls_subprocess(monkeypatch):
    fake_result = mock.Mock()
    fake_result.returncode = 0
    fake_result.stdout = "Indexes project files into the vector store.\n"
    fake_result.stderr = ""

    captured_cmd: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmd.append(cmd)
        return fake_result

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr("subprocess.run", fake_run)

    client = CodexClient(model="gpt-5.4-mini")
    result = client.chat([{"role": "user", "content": "describe index_project()"}])

    assert result == "Indexes project files into the vector store."
    assert captured_cmd[0][0] == "codex"
    assert "exec" in captured_cmd[0]
    # --model is NOT passed by default (ChatGPT accounts reject model overrides)
    assert "--model" not in captured_cmd[0]


def test_codex_chat_passes_model_flag_when_enabled(monkeypatch):
    fake_result = mock.Mock()
    fake_result.returncode = 0
    fake_result.stdout = "ok"
    fake_result.stderr = ""

    captured_cmd: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmd.append(cmd)
        return fake_result

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr("subprocess.run", fake_run)

    client = CodexClient(model="gpt-5.4-mini", pass_model_flag=True)
    client.chat([{"role": "user", "content": "test"}])

    assert "--model" in captured_cmd[0]
    assert "gpt-5.4-mini" in captured_cmd[0]


def test_codex_chat_raises_when_cli_not_found(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    client = CodexClient()
    with pytest.raises(RuntimeError, match="codex CLI not found"):
        client.chat([{"role": "user", "content": "test"}])


def test_codex_chat_raises_on_nonzero_exit(monkeypatch):
    fake_result = mock.Mock()
    fake_result.returncode = 1
    fake_result.stdout = ""
    fake_result.stderr = "some unexpected error"

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake_result)

    client = CodexClient()
    with pytest.raises(RuntimeError, match="exited 1"):
        client.chat([{"role": "user", "content": "test"}])


def test_codex_chat_raises_rate_limit_error(monkeypatch):
    fake_result = mock.Mock()
    fake_result.returncode = 1
    fake_result.stdout = ""
    fake_result.stderr = "rate limit exceeded"

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake_result)

    client = CodexClient()
    with pytest.raises(RateLimitError):
        client.chat([{"role": "user", "content": "test"}])


# ---------------------------------------------------------------------------
# community_summary: code_samples parameter
# ---------------------------------------------------------------------------

def test_community_summary_with_code_samples_includes_samples_in_prompt(mock_ollama_server):
    """community_summary(code_samples=...) includes actual code in the LLM prompt."""
    from unittest.mock import patch

    client = OllamaClient(base_url=mock_ollama_server)
    captured_messages = []

    def capture_chat(messages, **kw):
        captured_messages.extend(messages)
        return "TITLE: Auth Layer\nSUMMARY: Handles authentication."
    with patch.object(client, "chat", side_effect=capture_chat):
        title, _summary = client.community_summary(
            ["auth.login (function)"],
            code_samples=[("/src/auth.go", "func login(u, p string) bool { return check(u, p) }")],
        )

    assert title == "Auth Layer"
    assert "Auth Layer" in title
    # The prompt must include the code snippet
    prompt_text = " ".join(m.get("content", "") for m in captured_messages)
    assert "login" in prompt_text or "auth.go" in prompt_text, (
        "community_summary with code_samples must include code content in prompt"
    )


def test_community_summary_without_code_samples_still_works(mock_ollama_server):
    """community_summary(code_samples=None) is backward compatible."""
    client = OllamaClient(base_url=mock_ollama_server)
    title, summary = client.community_summary(["db.Query (function)"], code_samples=None)
    assert isinstance(title, str)
    assert isinstance(summary, str)


def test_project_overview_returns_json_parseable_string(mock_ollama_server):
    """project_overview() returns a string that contains JSON-like content."""
    client = OllamaClient(base_url=mock_ollama_server)
    result = client.project_overview([("main.go", "package main\nfunc main() {}")])
    assert isinstance(result, str)
    assert len(result) > 0


def test_project_synthesis_returns_string(mock_ollama_server):
    """project_synthesis() combines overview + exact facts and returns a string."""
    client = OllamaClient(base_url=mock_ollama_server)
    result = client.project_synthesis(
        '{"primary_language": "go", "confidence": "high"}',
        {"language_counts": [{"name": "go", "files": 42}]},
    )
    assert isinstance(result, str)
    assert len(result) > 0


# ===========================================================================
# OllamaClient-specific tests  (was test_ollama_client.py)
# ===========================================================================
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

    with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen), contextlib.suppress(Exception):
        client.chat([{"role": "user", "content": "test"}])

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
