"""E2E tests for the ClaudeCodeClient LLM provider.

These tests shell out to the locally installed `claude` CLI.
They are skipped automatically when the CLI is not installed.

Run manually:
    .venv/bin/pytest src/tests/test_e2e_mcp_claude_code.py -v -s
"""
from __future__ import annotations

import shutil

import pytest

# ---------------------------------------------------------------------------
# Shared skip guard
# ---------------------------------------------------------------------------

_claude_available = shutil.which("claude") is not None

pytestmark = pytest.mark.skipif(
    not _claude_available,
    reason="claude CLI not installed — install from https://claude.ai/code",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def claude_client():
    """Return a ClaudeCodeClient pointed at the fast Haiku model."""
    from opencode_search.enricher.client import ClaudeCodeClient

    return ClaudeCodeClient(model="claude-haiku-4-5-20251001", timeout=60)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_claude_code_client_is_available(claude_client) -> None:
    """ClaudeCodeClient.is_available() returns True when claude CLI exists."""
    assert claude_client.is_available(), (
        "ClaudeCodeClient.is_available() returned False despite claude CLI being present"
    )


def test_claude_code_can_describe_function(claude_client) -> None:
    """Use claude CLI to generate a function description — verifies LLM client works."""
    result = claude_client.symbol_intent(
        name="authenticate",
        signature="authenticate(token: str, db: Session) -> User | None",
        docstring="Check JWT token and return user if valid.",
    )
    assert isinstance(result, str), f"Expected str, got {type(result)}"
    assert len(result) > 10, f"Response too short: {result!r}"


def test_claude_code_returns_non_empty_string(claude_client) -> None:
    """A bare chat() call must return a non-empty string."""
    result = claude_client.chat(
        [{"role": "user", "content": "Reply with the single word: OK"}],
        max_tokens=16,
    )
    assert isinstance(result, str)
    assert result.strip(), "claude CLI returned empty output"


def test_claude_code_can_summarize_community(claude_client) -> None:
    """community_summary() must return a (title, summary) tuple of non-empty strings."""
    title, summary = claude_client.community_summary(
        node_summaries=[
            "authenticate (function): validates JWT tokens",
            "create_session (function): creates user session",
            "logout (function): invalidates session",
            "refresh_token (function): renews expiring JWT tokens",
        ]
    )
    assert isinstance(title, str) and len(title) > 0, (
        f"community_summary returned empty title: {title!r}"
    )
    assert isinstance(summary, str) and len(summary) > 0, (
        f"community_summary returned empty summary: {summary!r}"
    )


def test_claude_code_symbol_intent_no_docstring(claude_client) -> None:
    """symbol_intent() must work when docstring is None."""
    result = claude_client.symbol_intent(
        name="handle_search_code",
        signature="handle_search_code(query: str, project_paths: list[str], top_k: int) -> dict",
        docstring=None,
    )
    assert isinstance(result, str) and len(result) > 10, (
        f"symbol_intent without docstring returned bad result: {result!r}"
    )


def test_claude_code_from_env_returns_none_when_provider_not_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ClaudeCodeClient.from_env() returns None unless provider=claude-code."""
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "ollama")
    from opencode_search.enricher.client import ClaudeCodeClient

    client = ClaudeCodeClient.from_env()
    assert client is None, "from_env() should return None when provider != claude-code"


def test_claude_code_from_env_returns_client_when_provider_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ClaudeCodeClient.from_env() returns a client when provider=claude-code."""
    monkeypatch.setenv("OPENCODE_LLM_PROVIDER", "claude-code")
    from opencode_search.enricher.client import ClaudeCodeClient

    client = ClaudeCodeClient.from_env()
    assert isinstance(client, ClaudeCodeClient), (
        "from_env() should return ClaudeCodeClient when OPENCODE_LLM_PROVIDER=claude-code"
    )
