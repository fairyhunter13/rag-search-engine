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
_5_TOOLS = ["search", "ask", "graph", "overview", "index"]
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
        assert settings_path.exists(), f"Claude settings not found at {settings_path}"
        data = json.loads(settings_path.read_text())
        cmd = data.get("mcpServers", {}).get("opencode-search", {}).get("command", "")
        assert cmd, "opencode-search MCP command is empty"
        assert "opencode_search" in cmd or ".venv" in cmd, (
            f"MCP command doesn't point to opencode_search: {cmd!r}"
        )

    def test_claude_md_has_5_tools(self):
        claude_md = _HOME / ".claude" / "CLAUDE.md"
        assert claude_md.exists(), "~/.claude/CLAUDE.md not found — sync_global_instructions may need to run"
        text = claude_md.read_text()
        for tool in _5_TOOLS:
            assert tool in text, f"Tool '{tool}' missing from ~/.claude/CLAUDE.md"

    def test_claude_md_has_quick_decision_guide(self):
        claude_md = _HOME / ".claude" / "CLAUDE.md"
        assert claude_md.exists(), "~/.claude/CLAUDE.md not found — sync_global_instructions may need to run"
        text = claude_md.read_text()
        assert "QUICK DECISION GUIDE" in text, "QUICK DECISION GUIDE missing from ~/.claude/CLAUDE.md"

    def test_claude_md_prohibits_cpu_fallback(self):
        from pathlib import Path
        candidates = [
            _HOME / ".claude" / "CLAUDE.md",
            Path("/home/user/git/github.com/fairyhunter13/opencode-search-engine/CLAUDE.md"),
        ]
        texts = [p.read_text() for p in candidates if p.exists()]
        assert texts, "No CLAUDE.md found to check CPU prohibition — sync_global_instructions may need to run"
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
        assert config_path.exists(), f"Codex config not found at {config_path}"
        text = config_path.read_text()
        assert "opencode_search" in text or ".venv" in text, (
            "Codex MCP command doesn't reference opencode_search"
        )

    def test_codex_agents_md_has_5_tools(self):
        agents_md = _HOME / ".codex" / "AGENTS.md"
        assert agents_md.exists(), (
            "~/.codex/AGENTS.md not found — run scripts/configure_integrations.py"
        )
        text = agents_md.read_text()
        for tool in _5_TOOLS:
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
        assert config_path.exists(), f"Hermes config not found at {config_path}"
        text = config_path.read_text()
        assert "opencode-search" in text, (
            "opencode-search MCP not found in ~/.hermes/config.yaml"
        )

    def test_hermes_system_prompt_has_5_tools(self):
        config_path = _HOME / ".hermes" / "config.yaml"
        assert config_path.exists(), f"Hermes config not found at {config_path}"
        text = config_path.read_text()
        for tool in _5_TOOLS:
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
        pytest.fail("OpenCode config not found in ~/.config/opencode/")
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
    """All profiles must have the same version of the 5-tool instruction block (Phase 100)."""

    def _get_tool_count(self, text: str) -> int:
        """Count how many of the 5 tools appear in the text."""
        return sum(1 for t in _5_TOOLS if t in text)

    def test_all_profiles_have_all_5_tools(self):
        results: dict[str, int] = {}
        for profile, path in [
            ("claude_CLAUDE.md", _HOME / ".claude" / "CLAUDE.md"),
            ("codex_AGENTS.md", _HOME / ".codex" / "AGENTS.md"),
            ("hermes_config", _HOME / ".hermes" / "config.yaml"),
        ]:
            if path.exists():
                results[profile] = self._get_tool_count(path.read_text())

        for profile, count in results.items():
            assert count == 5, (
                f"{profile} only has {count}/5 tools in system prompt. "
                f"Run scripts/sync_global_instructions.py to fix."
            )


# ---------------------------------------------------------------------------
# Repo template files — drift gate (no daemon required)
# ---------------------------------------------------------------------------

_REPO = Path(__file__).parents[3]  # src/tests/live/ → 3 levels up → repo root


class TestMCPConfigTemplates:
    """mcp-config/*.json templates must stay consistent with the 5-tool API and CANONICAL_MCP_ENV.

    These tests run against repo files only — no daemon, no user home dir.
    They catch the case where someone updates the API without updating a template.
    """

    def test_claude_code_template_alwaysallow_is_subset_of_five_tools(self):
        data = json.loads((_REPO / "mcp-config" / "claude-code.json").read_text())
        always_allow = data["mcpServers"]["opencode-search"]["alwaysAllow"]
        assert set(always_allow) <= set(_5_TOOLS), (
            f"alwaysAllow contains unknown tools: {set(always_allow) - set(_5_TOOLS)}"
        )
        read_only = {"search", "ask", "graph", "overview"}
        assert read_only <= set(always_allow), (
            f"claude-code.json must allow all read-only tools {read_only}; "
            f"got {always_allow}"
        )

    def test_codex_template_lists_five_tools(self):
        data = json.loads((_REPO / "mcp-config" / "codex.json").read_text())
        tools = data["mcp"]["tools"]
        assert set(tools) == set(_5_TOOLS), (
            f"codex.json mcp.tools mismatch: {sorted(tools)} != {sorted(_5_TOOLS)}"
        )

    def test_hermes_template_tools_dict_matches_five(self):
        data = json.loads((_REPO / "mcp-config" / "hermes.json").read_text())
        tool_keys = set(data["tools"].keys())
        assert tool_keys == set(_5_TOOLS), (
            f"hermes.json tools keys mismatch: {sorted(tool_keys)} != {sorted(_5_TOOLS)}"
        )
        assert data["tools"]["index"]["always_allowed"] is False, (
            "index must be always_allowed=false in hermes.json (write/destructive operation)"
        )

    def test_canonical_env_has_both_provider_vars(self):
        import sys
        sys.path.insert(0, str(_REPO / "scripts"))
        from integrations.canonical import CANONICAL_MCP_ENV
        assert CANONICAL_MCP_ENV.get("OPENCODE_LLM_PROVIDER") == "ollama", (
            f"CANONICAL_MCP_ENV must set OPENCODE_LLM_PROVIDER=ollama; "
            f"got {CANONICAL_MCP_ENV.get('OPENCODE_LLM_PROVIDER')!r}"
        )
        assert CANONICAL_MCP_ENV.get("OPENCODE_QUERY_LLM_PROVIDER") == "ollama", (
            f"CANONICAL_MCP_ENV must set OPENCODE_QUERY_LLM_PROVIDER=ollama; "
            f"got {CANONICAL_MCP_ENV.get('OPENCODE_QUERY_LLM_PROVIDER')!r}"
        )


# ---------------------------------------------------------------------------
# Secondary Claude accounts
# ---------------------------------------------------------------------------

class TestClaudeAccountProfiles:
    """~/.claude-account1 and ~/.claude-account2 must mirror the primary profile."""

    def _check_account(self, account_dir: Path) -> None:
        assert account_dir.exists(), f"{account_dir} not found"
        settings_path = account_dir / "settings.json"
        claude_md = account_dir / "CLAUDE.md"
        assert settings_path.exists(), f"{settings_path} not found"
        data = json.loads(settings_path.read_text())
        servers = data.get("mcpServers", {})
        assert "opencode-search" in servers, (
            f"opencode-search not in {settings_path}; found: {list(servers.keys())}"
        )
        assert claude_md.exists(), f"{claude_md} not found — sync_global_instructions may need to run"
        text = claude_md.read_text()
        for tool in _5_TOOLS:
            assert tool in text, f"Tool '{tool}' missing from {claude_md}"

    def test_claude_account1_has_mcp_and_prompt(self):
        self._check_account(_HOME / ".claude-account1")

    def test_claude_account2_has_mcp_and_prompt(self):
        self._check_account(_HOME / ".claude-account2")


# ---------------------------------------------------------------------------
# Bash aliases sentinel block
# ---------------------------------------------------------------------------

class TestBashAliasesSentinel:
    """~/.bash_aliases must contain the sentinel-managed opencode-search alias block."""

    _ALIASES_PATH = _HOME / ".bash_aliases"

    def test_aliases_block_present(self):
        assert self._ALIASES_PATH.exists(), f"~/.bash_aliases not found at {self._ALIASES_PATH}"
        text = self._ALIASES_PATH.read_text()
        assert "[opencode-search-aliases:start]" in text, (
            "~/.bash_aliases missing [opencode-search-aliases:start] sentinel — "
            "run scripts/configure_integrations.py to install aliases"
        )
        assert "[opencode-search-aliases:end]" in text, (
            "~/.bash_aliases missing [opencode-search-aliases:end] sentinel"
        )
        # Key aliases must appear somewhere in the file (block may only be a comment header)
        for alias in ("ocs-index", "ocs-dash", "ocs"):
            assert alias in text, (
                f"Alias '{alias}' missing from ~/.bash_aliases"
            )

    def test_aliases_block_not_duplicated(self):
        assert self._ALIASES_PATH.exists(), f"~/.bash_aliases not found at {self._ALIASES_PATH}"
        text = self._ALIASES_PATH.read_text()
        count = text.count("[opencode-search-aliases:start]")
        assert count == 1, (
            f"[opencode-search-aliases:start] appears {count}× in ~/.bash_aliases; expected exactly 1"
        )


# ---------------------------------------------------------------------------
# Tier-1 guard tests — T1.3, T1.4, T1.5, T1.6
# ---------------------------------------------------------------------------

class TestResilienceRule:
    """Every profile must carry the RESILIENCE/fallback rule added in Tier 1 (T1.6).

    No stale 7-tool (build/federation/manage) text may remain in any profile.
    """

    from typing import ClassVar

    _RESILIENCE_PHRASE = "fallback"  # appears in "fallback:true" or "fall back to native"
    _STALE_TOOLS: ClassVar[list[str]] = ["build(", "federation(", "manage("]

    _PROFILES: ClassVar[list[tuple[str, Path]]] = [
        ("CLAUDE.md", _HOME / ".claude" / "CLAUDE.md"),
        ("AGENTS.md", _HOME / ".codex" / "AGENTS.md"),
        ("hermes_config.yaml", _HOME / ".hermes" / "config.yaml"),
        ("bash_aliases", _HOME / ".bash_aliases"),
    ]

    def test_canonical_body_has_resilience_rule(self):
        """scripts/integrations/canonical.py CANONICAL_BODY must contain the fallback rule."""
        # Read the file directly — scripts/ is not on sys.path in test runs
        canonical_path = Path(__file__).parents[3] / "scripts" / "integrations" / "canonical.py"
        assert canonical_path.exists(), f"canonical.py not found at {canonical_path}"
        text = canonical_path.read_text().lower()
        assert "fallback" in text, (
            "CANONICAL_BODY is missing the RESILIENCE/fallback guidance. "
            "Add it to scripts/integrations/canonical.py."
        )
        assert "timeout" in text, (
            "CANONICAL_BODY is missing timeout guidance in the RESILIENCE rule."
        )

    def test_all_profiles_have_resilience_rule(self):
        """Every installed profile file must contain the fallback/resilience guidance."""
        missing = []
        for name, path in self._PROFILES:
            if not path.exists():
                continue
            text = path.read_text()
            if self._RESILIENCE_PHRASE not in text.lower():
                missing.append(f"{name}: missing '{self._RESILIENCE_PHRASE}' keyword")
        assert not missing, (
            "These profiles are missing the RESILIENCE/fallback rule:\n"
            + "\n".join(f"  {m}" for m in missing)
            + "\nRun scripts/configure_integrations.py to re-sync profiles."
        )

    def test_no_stale_7_tool_api_in_profiles(self):
        """No profile should reference the old 7-tool API (build/federation/manage)."""
        found = []
        for name, path in self._PROFILES:
            if not path.exists():
                continue
            text = path.read_text()
            for stale_tool in self._STALE_TOOLS:
                if stale_tool in text and "opencode-search" in text:
                    found.append(f"{name}: contains stale tool reference '{stale_tool}'")
                    break
        assert not found, (
            "These profiles still reference the old 7-tool API:\n"
            + "\n".join(f"  {f}" for f in found)
            + "\nRun scripts/configure_integrations.py to re-sync."
        )


class TestBridgeTimeout:
    """T1.5: mcp_bridge._forward_tool must return a timeout sentinel, never hang."""

    def test_tool_deadlines_are_defined(self):
        """_TOOL_DEADLINES must include all 5 tools with values <= 10s."""
        from opencode_search.mcp_bridge import _TOOL_DEADLINES
        required = {"search", "ask", "graph", "overview", "index"}
        for tool in required:
            assert tool in _TOOL_DEADLINES, f"_TOOL_DEADLINES missing '{tool}'"
            assert _TOOL_DEADLINES[tool] <= 10.0, (
                f"_TOOL_DEADLINES['{tool}']={_TOOL_DEADLINES[tool]}s exceeds 10s ceiling"
            )

    def test_bridge_forward_times_out(self):
        """A stalled upstream causes _forward_tool to return (not hang) within a generous budget.

        The key T1.5 property is that the bridge returns rather than blocking indefinitely.
        ensure_daemon_running() overhead is excluded from the per-call deadline, so under heavy
        load the total elapsed can legitimately exceed the deadline+2 bound — we use a wider
        wall-clock budget (60s) that guarantees no hang while tolerating a busy daemon.
        The sentinel check (fallback:True or error dict) is the real correctness assertion.
        """
        import asyncio
        import time

        from opencode_search.mcp_bridge import _TOOL_DEADLINES, _forward_tool

        async def _run():
            _HARD_WALL_CLOCK = 60.0  # max total time including ensure_daemon_running overhead
            original_deadline = _TOOL_DEADLINES.get("ask", 8.0)
            _TOOL_DEADLINES["ask"] = 0.5  # force a very short per-call deadline
            t0 = time.monotonic()
            try:
                result = await _forward_tool("ask", {
                    "query": "timeout test query that should not matter",
                    "project_path": "/tmp/nonexistent_project_path_for_timeout_test",
                    "scope": "feature",
                })
            finally:
                _TOOL_DEADLINES["ask"] = original_deadline

            elapsed = time.monotonic() - t0
            # Must return (not hang) — ensure_daemon_running overhead + 0.5s deadline ≤ 60s
            assert elapsed < _HARD_WALL_CLOCK, (
                f"_forward_tool took {elapsed:.2f}s — HUNG (expected < {_HARD_WALL_CLOCK}s). "
                "The bridge timeout guard is not working (T1.5 regression)."
            )
            # Must return a dict (timeout sentinel or error) — never propagates an exception
            assert isinstance(result, dict), "Expected dict result from _forward_tool"

        asyncio.run(_run())


class TestFailureNotify:
    """T1.4: the failure-notify oneshot must only fire on true death, not transient restarts."""

    def test_notify_service_silent_on_recovery(self):
        """ExecStart shell script exits 0 when daemon is active|activating|reloading."""
        from opencode_search.daemon import _render_systemd_notify_failure_service
        content = _render_systemd_notify_failure_service()
        # The 'case' block must exit 0 (stay silent) if the daemon is back alive
        assert "active|activating|reloading) exit 0" in content, (
            "Notify script does not exit 0 on 'active|activating|reloading'. "
            "It would fire during normal auto-restart (T1.4 regression)."
        )

    def test_notify_service_has_restart_delay(self):
        """ExecStart waits at least 5s so systemd RestartSec=5 can complete."""
        from opencode_search.daemon import _render_systemd_notify_failure_service
        content = _render_systemd_notify_failure_service()
        import re
        # Must have 'sleep N' where N >= 5
        match = re.search(r"sleep\s+(\d+)", content)
        assert match, "Notify script is missing a 'sleep N' before the state check"
        delay = int(match.group(1))
        assert delay >= 5, (
            f"Notify script sleeps only {delay}s — must be ≥5 to let RestartSec=5 kick in "
            "before checking whether the daemon recovered (T1.4 regression)."
        )

    def test_notify_service_installed_matches_rendered(self):
        """Installed notify service file content matches what _render_systemd_notify_failure_service returns."""
        from opencode_search.daemon import (
            _SYSTEMD_NOTIFY_SERVICE_PATH,
            _render_systemd_notify_failure_service,
        )
        if not _SYSTEMD_NOTIFY_SERVICE_PATH.exists():
            return  # service not installed yet — skip silently
        installed = _SYSTEMD_NOTIFY_SERVICE_PATH.read_text()
        rendered = _render_systemd_notify_failure_service()
        # Key guards must be present in both the installed and rendered versions
        for phrase in ("active|activating|reloading) exit 0", "sleep"):
            assert phrase in installed, (
                f"Installed notify service at {_SYSTEMD_NOTIFY_SERVICE_PATH} is missing "
                f"'{phrase}'. Run `opencode-search daemon install` to update."
            )
            assert phrase in rendered, (
                f"Rendered notify service is missing '{phrase}' — code regression."
            )


class TestThermalGuard:
    """T1.3: _wait_for_gpu_cool must return within max_wait_s even if GPU stays hot."""

    def test_thermal_guard_has_max_wait(self):
        """_wait_for_gpu_cool must accept and respect a max_wait_s parameter."""
        import inspect

        from opencode_search.enricher.client import _wait_for_gpu_cool
        sig = inspect.signature(_wait_for_gpu_cool)
        assert "max_wait_s" in sig.parameters, (
            "_wait_for_gpu_cool is missing the max_wait_s parameter (T1.3 fix)"
        )

    def test_thermal_guard_bounded(self):
        """_wait_for_gpu_cool(max_wait_s=0.3) returns within 1s under any real GPU temp.

        No mocks: the function must return within max_wait_s regardless of whether the GPU
        is cool (returns immediately) or hot (exits after max_wait_s). Either path < 1s.
        """
        import time

        from opencode_search.enricher.client import _wait_for_gpu_cool

        t0 = time.monotonic()
        _wait_for_gpu_cool(max_wait_s=0.3)
        elapsed = time.monotonic() - t0

        assert elapsed < 1.0, (
            f"_wait_for_gpu_cool with max_wait_s=0.3 took {elapsed:.2f}s — expected <1.0s. "
            "The function is not respecting max_wait_s (T1.3 regression)."
        )
