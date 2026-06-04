"""MCP config + system prompt verification for all AI profiles.

Verifies that every AI client profile that should have opencode-search wired in
actually has it — and that the system prompt / AGENTS.md contains the 7-tool
instruction set with the QUICK DECISION GUIDE.

Profiles checked:
  claude     ~/.claude/settings.json  +  ~/.claude/CLAUDE.md
  codex      ~/.codex/config.toml     +  ~/.codex/AGENTS.md
  hermes     ~/.hermes/config.yaml
  opencode   ~/.config/opencode/

These tests do NOT require the daemon to be running — they verify on-disk config.
They are marked live because a wrong config means MCP tools silently don't work.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_HOME = Path.home()
_7_TOOLS = ["search", "ask", "graph", "overview", "build", "federation", "manage"]
_EXPECTED_CONTENT = ["QUICK DECISION GUIDE", "opencode-search"]


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

class TestClaudeProfile:
    """~/.claude/settings.json and ~/.claude/CLAUDE.md."""

    def test_claude_settings_has_mcp_server(self):
        settings_path = _HOME / ".claude" / "settings.json"
        assert settings_path.exists(), f"Claude settings not found at {settings_path}"
        data = json.loads(settings_path.read_text())
        servers = data.get("mcpServers", {})
        assert "opencode-search" in servers, (
            f"opencode-search not in claude mcpServers; found: {list(servers.keys())}"
        )

    def test_claude_mcp_command_points_to_venv(self):
        settings_path = _HOME / ".claude" / "settings.json"
        if not settings_path.exists():
            pytest.skip("Claude settings not found")
        data = json.loads(settings_path.read_text())
        cmd = data.get("mcpServers", {}).get("opencode-search", {}).get("command", "")
        assert cmd, "opencode-search MCP command is empty"
        assert "opencode_search" in cmd or ".venv" in cmd, (
            f"MCP command doesn't point to opencode_search: {cmd!r}"
        )

    def test_claude_md_has_7_tools(self):
        claude_md = _HOME / ".claude" / "CLAUDE.md"
        assert claude_md.exists(), "~/.claude/CLAUDE.md not found — sync_global_instructions may need to run"
        text = claude_md.read_text()
        for tool in _7_TOOLS:
            assert tool in text, f"Tool '{tool}' missing from ~/.claude/CLAUDE.md"

    def test_claude_md_has_quick_decision_guide(self):
        claude_md = _HOME / ".claude" / "CLAUDE.md"
        if not claude_md.exists():
            pytest.skip("~/.claude/CLAUDE.md not found")
        text = claude_md.read_text()
        assert "QUICK DECISION GUIDE" in text, "QUICK DECISION GUIDE missing from ~/.claude/CLAUDE.md"

    def test_claude_md_prohibits_cpu_fallback(self):
        from pathlib import Path
        candidates = [
            _HOME / ".claude" / "CLAUDE.md",
            Path("/home/user/git/github.com/fairyhunter13/opencode-search-engine/CLAUDE.md"),
        ]
        texts = [p.read_text() for p in candidates if p.exists()]
        if not texts:
            pytest.skip("No CLAUDE.md found to check CPU prohibition")
        combined = "\n".join(texts)
        assert "CPU" in combined and ("forbidden" in combined.lower() or "prohibited" in combined.lower()), (
            "CPU fallback prohibition not found in any CLAUDE.md"
        )


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

class TestCodexProfile:
    """~/.codex/config.toml and ~/.codex/AGENTS.md."""

    def test_codex_config_has_mcp_server(self):
        config_path = _HOME / ".codex" / "config.toml"
        assert config_path.exists(), f"Codex config not found at {config_path}"
        text = config_path.read_text()
        assert "opencode-search" in text, (
            "opencode-search not in codex config.toml"
        )

    def test_codex_config_command_points_to_venv(self):
        config_path = _HOME / ".codex" / "config.toml"
        if not config_path.exists():
            pytest.skip("Codex config not found")
        text = config_path.read_text()
        assert "opencode_search" in text or ".venv" in text, (
            "Codex MCP command doesn't reference opencode_search"
        )

    def test_codex_agents_md_has_7_tools(self):
        agents_md = _HOME / ".codex" / "AGENTS.md"
        assert agents_md.exists(), (
            "~/.codex/AGENTS.md not found — run scripts/configure_integrations.py"
        )
        text = agents_md.read_text()
        for tool in _7_TOOLS:
            assert tool in text, f"Tool '{tool}' missing from ~/.codex/AGENTS.md"

    def test_codex_has_developer_instructions_or_agents_md(self):
        config_path = _HOME / ".codex" / "config.toml"
        agents_md = _HOME / ".codex" / "AGENTS.md"
        has_dev_instructions = config_path.exists() and "developer_instructions" in config_path.read_text()
        has_agents = agents_md.exists() and "opencode-search" in agents_md.read_text()
        assert has_dev_instructions or has_agents, (
            "Codex has neither developer_instructions in config.toml nor AGENTS.md with opencode-search"
        )


# ---------------------------------------------------------------------------
# Hermes
# ---------------------------------------------------------------------------

class TestHermesProfile:
    """~/.hermes/config.yaml."""

    def test_hermes_config_has_mcp_server(self):
        config_path = _HOME / ".hermes" / "config.yaml"
        if not config_path.exists():
            pytest.skip("Hermes not installed (~/.hermes/config.yaml not found)")
        text = config_path.read_text()
        assert "opencode-search" in text, (
            "opencode-search MCP not found in ~/.hermes/config.yaml"
        )

    def test_hermes_system_prompt_has_7_tools(self):
        config_path = _HOME / ".hermes" / "config.yaml"
        if not config_path.exists():
            pytest.skip("Hermes not installed")
        text = config_path.read_text()
        for tool in _7_TOOLS:
            assert tool in text, f"Tool '{tool}' missing from hermes config (system_prompt)"


# ---------------------------------------------------------------------------
# OpenCode
# ---------------------------------------------------------------------------

class TestOpenCodeProfile:
    """~/.config/opencode/ — verify MCP is wired for both personal and default profiles."""

    def _opencode_config_text(self) -> str:
        for candidate in [
            _HOME / ".config" / "opencode" / "opencode.jsonc",
            _HOME / ".config" / "opencode" / "config.json",
            _HOME / ".config" / "opencode" / "config.jsonc",
        ]:
            if candidate.exists():
                return candidate.read_text()
        pytest.skip("OpenCode config not found in ~/.config/opencode/")
        return ""

    def test_opencode_config_has_mcp_server(self):
        text = self._opencode_config_text()
        assert "opencode-search" in text, (
            "opencode-search MCP not found in OpenCode config"
        )

    def test_opencode_config_command_points_to_venv(self):
        text = self._opencode_config_text()
        assert "opencode_search" in text or ".venv" in text, (
            "OpenCode MCP command doesn't reference opencode_search"
        )


# ---------------------------------------------------------------------------
# Sync script: global instructions in sync
# ---------------------------------------------------------------------------

class TestGlobalInstructionSync:
    """All profiles must have the same version of the 7-tool instruction block."""

    def _get_tool_count(self, text: str) -> int:
        """Count how many of the 7 tools appear in the text."""
        return sum(1 for t in _7_TOOLS if t in text)

    def test_all_profiles_have_all_7_tools(self):
        results: dict[str, int] = {}
        for profile, path in [
            ("claude_CLAUDE.md", _HOME / ".claude" / "CLAUDE.md"),
            ("codex_AGENTS.md", _HOME / ".codex" / "AGENTS.md"),
            ("hermes_config", _HOME / ".hermes" / "config.yaml"),
        ]:
            if path.exists():
                results[profile] = self._get_tool_count(path.read_text())

        for profile, count in results.items():
            assert count == 7, (
                f"{profile} only has {count}/7 tools in system prompt. "
                f"Run scripts/sync_global_instructions.py to fix."
            )
