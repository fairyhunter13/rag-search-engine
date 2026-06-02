"""E2E tests for LLM providers: ClaudeCodeClient (Haiku) and CodexClient (gpt-5.4-mini)."""
from __future__ import annotations

import os
import shutil

import pytest

_claude_available = shutil.which("claude") is not None
_codex_available = shutil.which("codex") is not None and bool(
    os.environ.get("OPENCODE_RUN_CODEX_TESTS", "")
)

_skip_claude = pytest.mark.runtime_deps
_skip_codex = pytest.mark.runtime_deps


# === ClaudeCodeClient (claude-haiku) ===

@_skip_claude
class TestClaudeCodeClientHaiku:
    @pytest.fixture(scope="class")
    def client(self):
        from opencode_search.enricher.client import ClaudeCodeClient
        return ClaudeCodeClient(model="claude-haiku-4-5-20251001", timeout=90)

    def test_claude_code_client_is_available(self, client) -> None:
        assert client.is_available()

    def test_claude_haiku_can_describe_function(self, client) -> None:
        result = client.symbol_intent(
            name="authenticate",
            signature="authenticate(token: str, db: Session) -> User | None",
            docstring="Check JWT token and return user if valid.",
        )
        assert isinstance(result, str) and len(result) > 10

    def test_claude_haiku_returns_non_empty_string(self, client) -> None:
        result = client.chat(
            [{"role": "user", "content": "Reply with the single word: OK"}], max_tokens=16
        )
        assert isinstance(result, str) and result.strip()

    def test_claude_haiku_can_summarize_community(self, client) -> None:
        title, summary = client.community_summary(
            node_summaries=[
                "authenticate (function): validates JWT tokens",
                "create_session (function): creates user session",
                "logout (function): invalidates session",
                "refresh_token (function): renews expiring JWT tokens",
            ]
        )
        assert isinstance(title, str) and len(title) > 0
        assert isinstance(summary, str) and len(summary) > 0

    def test_claude_haiku_symbol_intent_no_docstring(self, client) -> None:
        result = client.symbol_intent(
            name="handle_search_code",
            signature="handle_search_code(query: str, project_paths: list[str], top_k: int) -> dict",
            docstring=None,
        )
        assert isinstance(result, str) and len(result) > 10

    def test_claude_haiku_module_wiki_page(self, client) -> None:
        page = client.module_wiki_page(
            module_path="opencode_search.handlers._query",
            symbols=["handle_search_code", "handle_search_docs"],
            imports=["opencode_search.search", "opencode_search.embeddings"],
        )
        assert isinstance(page, str) and len(page) > 50

    def test_claude_code_from_env_returns_none_when_provider_not_set(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "ollama")
        from opencode_search.enricher.client import ClaudeCodeClient
        assert ClaudeCodeClient.from_env() is None

    def test_claude_code_from_env_returns_client_when_provider_set(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "claude-code")
        from opencode_search.enricher.client import ClaudeCodeClient
        client = ClaudeCodeClient.from_env()
        assert isinstance(client, ClaudeCodeClient)


# === CodexClient (gpt-5.4-mini) ===

@_skip_codex
class TestCodexClientGptMini:
    @pytest.fixture(scope="class")
    def client(self):
        from opencode_search.enricher.client import CodexClient
        return CodexClient(model="gpt-5.4-mini", timeout=90)

    def test_codex_can_describe_function(self, client) -> None:
        result = client.symbol_intent(
            name="handle_search_code",
            signature="handle_search_code(query: str, project_paths: list[str], top_k: int) -> dict",
            docstring=None,
        )
        assert isinstance(result, str) and len(result) > 10

    def test_codex_can_summarize_community(self, client) -> None:
        title, summary = client.community_summary(
            node_summaries=[
                "embed_passages (function): batch-embed code snippets with GPU",
                "rerank_results (function): reorder hits by semantic relevance",
            ]
        )
        assert isinstance(title, str) and len(title) > 0
        assert isinstance(summary, str) and len(summary) > 0

    def test_codex_symbol_intent_no_docstring(self, client) -> None:
        result = client.symbol_intent(
            name="index_project",
            signature="index_project(project_path: str) -> dict",
            docstring=None,
        )
        assert isinstance(result, str) and len(result) > 10

    def test_codex_chat_returns_string(self, client) -> None:
        result = client.chat(
            [{"role": "user", "content": "Reply with the single word: OK"}], max_tokens=16
        )
        assert isinstance(result, str) and result.strip()

    def test_codex_from_env_returns_client(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "codex")
        from opencode_search.enricher.client import CodexClient
        assert isinstance(CodexClient.from_env(), CodexClient)

    def test_codex_from_env_returns_none_when_wrong_provider(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "ollama")
        from opencode_search.enricher.client import CodexClient
        assert CodexClient.from_env() is None


# === Factory tests (no CLI required) ===

class TestCreateLlmClientFactory:
    def test_factory_returns_none_for_none_provider(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "none")
        from opencode_search.enricher.client import create_llm_client
        assert create_llm_client() is None

    def test_factory_returns_ollama_client_for_ollama_provider(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "ollama")
        from opencode_search.enricher.client import OllamaClient, create_llm_client
        assert isinstance(create_llm_client(), OllamaClient)

    def test_factory_returns_claude_code_client(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "claude-code")
        from opencode_search.enricher.client import ClaudeCodeClient, create_llm_client
        assert isinstance(create_llm_client(), ClaudeCodeClient)

    def test_factory_returns_codex_client(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "codex")
        from opencode_search.enricher.client import CodexClient, FallbackLLMClient, create_llm_client
        client = create_llm_client()
        core = client.primary if isinstance(client, FallbackLLMClient) else client
        assert isinstance(core, CodexClient)

    def test_factory_raises_for_unknown_provider(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "invalid-provider")
        from opencode_search.enricher.client import create_llm_client
        with pytest.raises(ValueError, match="Unknown OPENCODE_LLM_PROVIDER"):
            create_llm_client()

    def test_factory_returns_codex_when_env_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("OPENCODE_LLM_PROVIDER", raising=False)
        from opencode_search.enricher.client import CodexClient, FallbackLLMClient, create_llm_client
        client = create_llm_client()
        core = client.primary if isinstance(client, FallbackLLMClient) else client
        assert isinstance(core, CodexClient)
