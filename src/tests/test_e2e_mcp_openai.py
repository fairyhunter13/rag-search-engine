"""E2E tests using Claude Code CLI (Haiku) and Codex CLI (gpt-5.4-mini).

No API keys required — authentication is managed by the installed CLIs.

Run manually:
    .venv/bin/pytest src/tests/test_e2e_mcp_openai.py -v -s
"""
from __future__ import annotations

import os
import shutil

import pytest

# ---------------------------------------------------------------------------
# Availability guards — skip entire class if CLI not present, never on API keys
# ---------------------------------------------------------------------------

_claude_available = shutil.which("claude") is not None
# codex integration tests require both the CLI and OPENCODE_RUN_CODEX_TESTS=1
# to prevent CI failures when the codex session has expired.
_codex_available = shutil.which("codex") is not None and bool(
    os.environ.get("OPENCODE_RUN_CODEX_TESTS", "")
)

_skip_claude = pytest.mark.skipif(
    not _claude_available,
    reason="claude CLI not installed — install from https://claude.ai/code",
)
_skip_codex = pytest.mark.skipif(
    not _codex_available,
    reason="codex CLI not installed or OPENCODE_RUN_CODEX_TESTS not set (set env var and ensure 'codex login' is done)",
)


# ---------------------------------------------------------------------------
# ClaudeCodeClient (Haiku) tests — replaces direct Anthropic API tests
# ---------------------------------------------------------------------------


@_skip_claude
class TestClaudeCodeClientHaiku:
    """Full integration tests using `claude -p` with claude-haiku-4-5-20251001."""

    @pytest.fixture(scope="class")
    def client(self):
        from opencode_search.enricher.client import ClaudeCodeClient
        return ClaudeCodeClient(model="claude-haiku-4-5-20251001", timeout=90)

    def test_claude_haiku_can_describe_function(self, client) -> None:
        result = client.symbol_intent(
            name="index_project",
            signature="index_project(project_path: str) -> dict",
            docstring="Index all files in a project into the vector database.",
        )
        assert isinstance(result, str) and len(result) > 10, (
            f"symbol_intent returned bad result: {result!r}"
        )

    def test_claude_haiku_can_summarize_community(self, client) -> None:
        title, summary = client.community_summary(
            node_summaries=[
                "handle_wiki_generate (function): generates wiki pages for communities",
                "handle_wiki_ingest (function): ingests raw documents into wiki",
                "handle_wiki_query (function): searches wiki pages",
                "handle_wiki_lint (function): checks wiki health",
            ]
        )
        assert isinstance(title, str) and len(title) > 0
        assert isinstance(summary, str) and len(summary) > 0

    def test_claude_haiku_symbol_intent_no_docstring(self, client) -> None:
        result = client.symbol_intent(
            name="search_code",
            signature="search_code(query: str, project_paths: list[str] | None, top_k: int) -> dict",
            docstring=None,
        )
        assert isinstance(result, str) and len(result) > 10

    def test_claude_haiku_chat_returns_string(self, client) -> None:
        result = client.chat(
            [{"role": "user", "content": "Reply with the single word: OK"}],
            max_tokens=16,
        )
        assert isinstance(result, str) and result.strip()

    def test_claude_haiku_module_wiki_page(self, client) -> None:
        page = client.module_wiki_page(
            module_path="opencode_search.handlers._query",
            symbols=["handle_search_code", "handle_search_docs"],
            imports=["opencode_search.search", "opencode_search.embeddings"],
        )
        assert isinstance(page, str) and len(page) > 50

    def test_claude_haiku_from_env_returns_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "claude-code")
        from opencode_search.enricher.client import ClaudeCodeClient
        client = ClaudeCodeClient.from_env()
        assert isinstance(client, ClaudeCodeClient)

    def test_claude_haiku_from_env_returns_none_when_wrong_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "ollama")
        from opencode_search.enricher.client import ClaudeCodeClient
        assert ClaudeCodeClient.from_env() is None


# ---------------------------------------------------------------------------
# CodexClient (gpt-5.4-mini) tests — replaces direct OpenAI API tests
# ---------------------------------------------------------------------------


@_skip_codex
class TestCodexClientGptMini:
    """Full integration tests using `codex exec` with gpt-5.4-mini."""

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
        assert isinstance(result, str) and len(result) > 10, (
            f"symbol_intent returned bad result: {result!r}"
        )

    def test_codex_can_summarize_community(self, client) -> None:
        title, summary = client.community_summary(
            node_summaries=[
                "embed_passages (function): batch-embed code snippets with GPU",
                "rerank_results (function): reorder hits by semantic relevance",
                "load_model (function): load ONNX model into CUDA execution provider",
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
            [{"role": "user", "content": "Reply with the single word: OK"}],
            max_tokens=16,
        )
        assert isinstance(result, str) and result.strip()

    def test_codex_from_env_returns_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "codex")
        from opencode_search.enricher.client import CodexClient
        client = CodexClient.from_env()
        assert isinstance(client, CodexClient)

    def test_codex_from_env_returns_none_when_wrong_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "ollama")
        from opencode_search.enricher.client import CodexClient
        assert CodexClient.from_env() is None


# ---------------------------------------------------------------------------
# Factory tests (no CLI required)
# ---------------------------------------------------------------------------


class TestCreateLlmClientFactory:
    def test_factory_returns_none_for_none_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "none")
        from opencode_search.enricher.client import create_llm_client
        assert create_llm_client() is None

    def test_factory_returns_ollama_client_for_ollama_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "ollama")
        from opencode_search.enricher.client import OllamaClient, create_llm_client
        client = create_llm_client()
        assert isinstance(client, OllamaClient)

    def test_factory_returns_claude_code_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "claude-code")
        from opencode_search.enricher.client import ClaudeCodeClient, create_llm_client
        client = create_llm_client()
        assert isinstance(client, ClaudeCodeClient)

    def test_factory_returns_codex_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "codex")
        from opencode_search.enricher.client import CodexClient, create_llm_client
        client = create_llm_client()
        assert isinstance(client, CodexClient)

    def test_factory_raises_for_unknown_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "invalid-provider")
        from opencode_search.enricher.client import create_llm_client
        with pytest.raises(ValueError, match="Unknown OPENCODE_LLM_PROVIDER"):
            create_llm_client()

    def test_factory_returns_codex_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENCODE_LLM_PROVIDER", raising=False)
        from opencode_search.enricher.client import CodexClient, create_llm_client
        client = create_llm_client()
        assert isinstance(client, CodexClient)
