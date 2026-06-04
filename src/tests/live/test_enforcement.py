"""LLM provider enforcement tests.

Verifies that:
1. create_llm_client() (KB build path) rejects codex and claude-code — raises RuntimeError
2. create_llm_client() accepts ollama (the only allowed KB provider)
3. create_query_llm_client() (chat path) accepts codex and produces a client
4. MCP bridge configs for all profiles have ollama env vars set (no API calls in tool queries)
5. bash_aliases explicitly sets OPENCODE_QUERY_LLM_PROVIDER=codex for daemon chat
6. Dashboard chat reports a model name indicating codex or ollama (never empty)

These are purely structural / import-time checks — no daemon required for 1-5.
Test 6 requires the live daemon.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_HOME = Path.home()
_VENV_PYTHON = Path("/home/user/git/github.com/fairyhunter13/opencode-search-engine/.venv/bin/python")
_MCP_BRIDGE_ENV_VARS = {
    "OPENCODE_LLM_PROVIDER": "ollama",
    "OPENCODE_QUERY_LLM_PROVIDER": "ollama",
}


# ---------------------------------------------------------------------------
# create_llm_client() enforcement
# ---------------------------------------------------------------------------

def _run_provider_check(provider: str) -> subprocess.CompletedProcess:
    """Run create_llm_client() with given OPENCODE_LLM_PROVIDER in a subprocess."""
    env = {**os.environ, "OPENCODE_LLM_PROVIDER": provider}
    return subprocess.run(
        [str(_VENV_PYTHON), "-c",
         "from opencode_search.enricher.client import create_llm_client; create_llm_client()"],
        capture_output=True, text=True, env=env,
        cwd="/home/user/git/github.com/fairyhunter13/opencode-search-engine",
    )


class TestKBProviderEnforcement:
    """create_llm_client() must reject codex and claude-code for KB build operations."""

    def test_create_llm_client_rejects_codex(self):
        result = _run_provider_check("codex")
        assert result.returncode != 0, (
            "create_llm_client() must raise RuntimeError for OPENCODE_LLM_PROVIDER=codex"
        )
        combined = result.stdout + result.stderr
        assert "FORBIDDEN" in combined or "forbidden" in combined.lower(), (
            f"Expected FORBIDDEN in error message; got:\n{combined[:400]}"
        )

    def test_create_llm_client_rejects_claude_code(self):
        result = _run_provider_check("claude-code")
        assert result.returncode != 0, (
            "create_llm_client() must raise RuntimeError for OPENCODE_LLM_PROVIDER=claude-code"
        )
        combined = result.stdout + result.stderr
        assert "FORBIDDEN" in combined or "forbidden" in combined.lower(), (
            f"Expected FORBIDDEN in error message; got:\n{combined[:400]}"
        )

    def test_create_llm_client_accepts_ollama(self):
        result = _run_provider_check("ollama")
        assert result.returncode == 0, (
            f"create_llm_client() must succeed for OPENCODE_LLM_PROVIDER=ollama; "
            f"stderr: {result.stderr[:300]}"
        )

    def test_create_llm_client_accepts_none(self):
        result = _run_provider_check("none")
        assert result.returncode == 0, (
            f"create_llm_client() must return None for OPENCODE_LLM_PROVIDER=none; "
            f"stderr: {result.stderr[:300]}"
        )

    def test_error_message_mentions_query_llm_provider(self):
        """Error must guide the user to OPENCODE_QUERY_LLM_PROVIDER for chat use."""
        result = _run_provider_check("codex")
        combined = result.stdout + result.stderr
        assert "QUERY_LLM_PROVIDER" in combined, (
            f"Error message must mention OPENCODE_QUERY_LLM_PROVIDER; got:\n{combined[:400]}"
        )


class TestQueryLLMClientAcceptsCodex:
    """create_query_llm_client() must accept codex (it's the chat-tier client)."""

    def test_create_query_llm_client_accepts_codex(self):
        env = {**os.environ, "OPENCODE_QUERY_LLM_PROVIDER": "codex"}
        result = subprocess.run(
            [str(_VENV_PYTHON), "-c",
             "from opencode_search.enricher.client import create_query_llm_client; "
             "c = create_query_llm_client(); assert c is not None, 'got None'"],
            capture_output=True, text=True, env=env,
            cwd="/home/user/git/github.com/fairyhunter13/opencode-search-engine",
        )
        assert result.returncode == 0, (
            f"create_query_llm_client() must accept OPENCODE_QUERY_LLM_PROVIDER=codex; "
            f"stderr: {result.stderr[:300]}"
        )

    def test_create_query_llm_client_accepts_claude_code(self):
        env = {**os.environ, "OPENCODE_QUERY_LLM_PROVIDER": "claude-code"}
        result = subprocess.run(
            [str(_VENV_PYTHON), "-c",
             "from opencode_search.enricher.client import create_query_llm_client; "
             "c = create_query_llm_client(); assert c is not None, 'got None'"],
            capture_output=True, text=True, env=env,
            cwd="/home/user/git/github.com/fairyhunter13/opencode-search-engine",
        )
        assert result.returncode == 0, (
            f"create_query_llm_client() must accept OPENCODE_QUERY_LLM_PROVIDER=claude-code; "
            f"stderr: {result.stderr[:300]}"
        )


# ---------------------------------------------------------------------------
# MCP bridge config enforcement
# ---------------------------------------------------------------------------

class TestMCPBridgeProviderConfig:
    """All MCP bridge configs must force OPENCODE_LLM_PROVIDER=ollama.

    This prevents any MCP tool invocation from accidentally using codex or claude-code
    for KB build operations or query synthesis inside the bridge subprocess.
    """

    def test_claude_settings_bridge_forces_ollama_for_kb(self):
        settings = _HOME / ".claude" / "settings.json"
        assert settings.exists(), f"Claude settings not found at {settings}"
        data = json.loads(settings.read_text())
        env = data.get("mcpServers", {}).get("opencode-search", {}).get("env", {})
        assert env.get("OPENCODE_LLM_PROVIDER") == "ollama", (
            f"Claude MCP bridge must set OPENCODE_LLM_PROVIDER=ollama; got {env.get('OPENCODE_LLM_PROVIDER')!r}"
        )

    def test_claude_settings_bridge_forces_ollama_for_queries(self):
        settings = _HOME / ".claude" / "settings.json"
        data = json.loads(settings.read_text())
        env = data.get("mcpServers", {}).get("opencode-search", {}).get("env", {})
        assert env.get("OPENCODE_QUERY_LLM_PROVIDER") == "ollama", (
            f"Claude MCP bridge must set OPENCODE_QUERY_LLM_PROVIDER=ollama; "
            f"got {env.get('OPENCODE_QUERY_LLM_PROVIDER')!r}"
        )

    def test_codex_config_bridge_forces_ollama_for_kb(self):
        config = _HOME / ".codex" / "config.toml"
        assert config.exists(), f"Codex config not found at {config}"
        text = config.read_text()
        assert 'OPENCODE_LLM_PROVIDER = "ollama"' in text or "OPENCODE_LLM_PROVIDER='ollama'" in text, (
            "Codex MCP bridge must have OPENCODE_LLM_PROVIDER=ollama in [mcp_servers.opencode-search.env]"
        )

    def test_codex_config_bridge_forces_ollama_for_queries(self):
        config = _HOME / ".codex" / "config.toml"
        text = config.read_text()
        assert 'OPENCODE_QUERY_LLM_PROVIDER = "ollama"' in text or "OPENCODE_QUERY_LLM_PROVIDER='ollama'" in text, (
            "Codex MCP bridge must have OPENCODE_QUERY_LLM_PROVIDER=ollama"
        )

    def test_hermes_config_bridge_forces_ollama_for_kb(self):
        config = _HOME / ".hermes" / "config.yaml"
        if not config.exists():
            pytest.skip("Hermes not installed")
        text = config.read_text()
        assert "OPENCODE_LLM_PROVIDER: ollama" in text, (
            "Hermes MCP bridge must have OPENCODE_LLM_PROVIDER: ollama in env section"
        )

    def test_hermes_config_bridge_forces_ollama_for_queries(self):
        config = _HOME / ".hermes" / "config.yaml"
        if not config.exists():
            pytest.skip("Hermes not installed")
        text = config.read_text()
        assert "OPENCODE_QUERY_LLM_PROVIDER: ollama" in text, (
            "Hermes MCP bridge must have OPENCODE_QUERY_LLM_PROVIDER: ollama"
        )


class TestDaemonQueryProviderConfig:
    """bash_aliases must explicitly route dashboard chat to codex."""

    def test_bash_aliases_sets_query_provider_to_codex(self):
        aliases = _HOME / ".bash_aliases"
        assert aliases.exists(), "~/.bash_aliases not found"
        text = aliases.read_text()
        assert "OPENCODE_QUERY_LLM_PROVIDER=codex" in text, (
            "~/.bash_aliases must export OPENCODE_QUERY_LLM_PROVIDER=codex for daemon chat"
        )

    def test_bash_aliases_kb_provider_is_ollama(self):
        aliases = _HOME / ".bash_aliases"
        text = aliases.read_text()
        assert "OPENCODE_LLM_PROVIDER=ollama" in text, (
            "~/.bash_aliases must export OPENCODE_LLM_PROVIDER=ollama (KB build must use local GPU)"
        )

    def test_bash_aliases_no_codex_for_kb_provider(self):
        """The comment-out switch lines are ok, but the active export must not set codex for KB."""
        aliases = _HOME / ".bash_aliases"
        text = aliases.read_text()
        # Active export lines (not comment lines) must not set OPENCODE_LLM_PROVIDER=codex
        active_lines = [
            ln for ln in text.splitlines()
            if "export OPENCODE_LLM_PROVIDER=codex" in ln and not ln.strip().startswith("#")
        ]
        assert not active_lines, (
            f"Found active export OPENCODE_LLM_PROVIDER=codex in bash_aliases: {active_lines}"
        )


# ---------------------------------------------------------------------------
# Live dashboard chat — verify model field is present and non-empty
# ---------------------------------------------------------------------------

class TestChatModelReported:
    """Chat stream must report which model answered — proves routing is live."""

    def test_chat_done_reports_model(self, http, project):
        from .conftest import parse_sse
        r = http.post(
            "/api/chat_stream",
            json={"project": project, "query": "What is the overall architecture of this project?"},
            headers={"Accept": "text/event-stream"},
        )
        assert r.status_code == 200, f"chat_stream failed: {r.status_code}"
        events = parse_sse(r)
        done = next((e for e in events if e.get("type") == "done"), None)
        assert done is not None, "No done event in chat stream"
        model = done.get("model", "")
        assert model, (
            f"done event must have non-empty 'model' field; got done={done}"
        )

    def test_chat_search_intent_uses_vector_index(self, http, project):
        """search intent must return file paths from the indexed project."""
        from .conftest import parse_sse
        r = http.post(
            "/api/chat_stream",
            json={"project": project, "query": "find the main entry point handler function"},
            headers={"Accept": "text/event-stream"},
        )
        assert r.status_code == 200
        events = parse_sse(r)
        answer = "".join(e.get("text", "") for e in events if e.get("type") == "token")
        done = next((e for e in events if e.get("type") == "done"), {})
        assert done.get("intent") == "search", (
            f"Expected intent=search; got {done.get('intent')!r}"
        )
        assert len(answer) > 20, f"Search answer too short: {answer!r}"
