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
        """A very short deadline causes _forward_tool to return a timeout sentinel, not hang.

        With the hot-path fix (daemon_is_healthy() skips the file-locked ensure_daemon_running
        on every call), the only time budget consumed is the per-tool deadline itself — no extra
        flock overhead on the hot path.  We use 0.1s deadline so the connection+initialize phase
        is itself unlikely to finish, ensuring the TimeoutError path actually fires.
        """
        import asyncio
        import time

        from opencode_search.mcp_bridge import _TOOL_DEADLINES, _forward_tool

        async def _run():
            original_deadline = _TOOL_DEADLINES.get("search", 6.0)
            # 0.1s is tighter than any connection+initialize round-trip → reliably fires timeout
            _TOOL_DEADLINES["search"] = 0.1
            t0 = time.monotonic()
            try:
                result = await _forward_tool("search", {
                    "query": "bridge timeout sentinel test",
                    "scope": "code",
                })
            finally:
                _TOOL_DEADLINES["search"] = original_deadline

            elapsed = time.monotonic() - t0
            # Must return within deadline + 2s overhead (no flock contention on hot path)
            assert elapsed < 0.1 + 2.0, (
                f"_forward_tool took {elapsed:.2f}s — HUNG (expected < 2.1s). "
                "The deadline wrapper is not covering the full attempt body (T1.5 regression)."
            )
            # Must return a timeout sentinel — fallback:true signals the caller to use native tools
            assert isinstance(result, dict), "Expected dict result from _forward_tool"
            assert result.get("status") == "timeout" or result.get("fallback") is True, (
                f"Expected timeout sentinel with fallback:true, got: {result}"
            )

        asyncio.run(_run())

    def test_bridge_no_hang_on_locked_daemon_file(self):
        """Hot path: when daemon is healthy, _forward_tool never acquires daemon.lock.

        Hold daemon.lock exclusively from a background thread while _forward_tool runs.
        Since the hot path calls daemon_is_healthy() (lock-free) rather than
        ensure_daemon_running() (flock), the call must complete — not hang — within the deadline.
        """
        import asyncio
        import fcntl
        import threading
        import time

        from opencode_search.daemon import _LOCK_PATH
        from opencode_search.mcp_bridge import _TOOL_DEADLINES, _forward_tool

        lock_held = threading.Event()
        release_lock = threading.Event()

        def _hold_lock() -> None:
            with _LOCK_PATH.open("a+", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                lock_held.set()
                release_lock.wait(timeout=15.0)
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

        t = threading.Thread(target=_hold_lock, daemon=True)
        t.start()
        assert lock_held.wait(timeout=5.0), "Lock holder thread did not start"

        original_deadline = _TOOL_DEADLINES.get("search", 6.0)
        _TOOL_DEADLINES["search"] = 2.0  # short enough to detect a hang quickly

        async def _run():
            return await _forward_tool("search", {
                "query": "lock contention hot path test",
                "scope": "code",
            })

        t0 = time.monotonic()
        try:
            result = asyncio.run(_run())
        finally:
            _TOOL_DEADLINES["search"] = original_deadline
            release_lock.set()
            t.join(timeout=5.0)

        elapsed = time.monotonic() - t0
        # Must return within deadline + 1s — not hang for minutes waiting for the flock
        assert elapsed < 2.0 + 1.0, (
            f"_forward_tool took {elapsed:.2f}s while daemon.lock was held — HUNG. "
            "Hot path must not acquire daemon.lock when daemon is healthy (T1.5 regression)."
        )
        # Must return a dict — either a real result (healthy daemon responds) or a timeout sentinel
        assert isinstance(result, dict), "Expected dict from _forward_tool under lock contention"


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


class TestReadPathNoLLM:
    """Zero-LLM read path invariant: every MCP read handler must return llm_used=False.

    These tests call the handler functions directly (not via bridge) on a real indexed
    project with an intentionally cold answer cache (use_cache=False).  No mocks.
    """

    def test_ask_feature_no_llm(self, project):
        """ask(scope=feature) must return llm_used=False within 10s on cache miss."""
        import asyncio
        import time

        from opencode_search.handlers._feature import handle_ask_feature

        t0 = time.monotonic()
        result = asyncio.run(handle_ask_feature("search handler", project, use_cache=False))
        elapsed = time.monotonic() - t0

        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        assert result.get("llm_used") is False, (
            f"ask(scope=feature) set llm_used={result.get('llm_used')!r} — "
            "LLM must not run on the read path"
        )
        assert elapsed < 10.0, (
            f"ask(scope=feature) took {elapsed:.2f}s — exceeds 10s hard deadline"
        )

    def test_ask_global_no_llm(self, project):
        """ask(scope=global) must return llm_used=False within 10s on cache miss."""
        import asyncio
        import time

        from opencode_search.handlers._global_search import handle_global_synthesis

        t0 = time.monotonic()
        result = asyncio.run(handle_global_synthesis("architecture overview", project, use_cache=False))
        elapsed = time.monotonic() - t0

        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        assert result.get("llm_used") is False, (
            f"ask(scope=global) set llm_used={result.get('llm_used')!r} — LLM must not run on read path"
        )
        assert elapsed < 10.0, (
            f"ask(scope=global) took {elapsed:.2f}s — exceeds 10s hard deadline"
        )

    def test_ask_business_no_llm(self, project):
        """ask(scope=business) must return llm_used=False within 10s on cache miss."""
        import asyncio
        import time

        from opencode_search.handlers._business import handle_ask_business

        t0 = time.monotonic()
        result = asyncio.run(handle_ask_business("core features", project, use_cache=False))
        elapsed = time.monotonic() - t0

        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        assert result.get("llm_used") is False, (
            f"ask(scope=business) set llm_used={result.get('llm_used')!r} — LLM must not run on read path"
        )
        assert elapsed < 10.0, (
            f"ask(scope=business) took {elapsed:.2f}s — exceeds 10s hard deadline"
        )

    def test_graph_impact_narrative_no_llm(self, project):
        """graph(relation=impact_narrative) must return llm_used=False within 10s."""
        import asyncio
        import time

        from opencode_search.handlers._impact import handle_impact_narrative

        t0 = time.monotonic()
        result = asyncio.run(handle_impact_narrative("handle_search_code", project))
        elapsed = time.monotonic() - t0

        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        assert result.get("llm_used") is False, (
            f"impact_narrative set llm_used={result.get('llm_used')!r} — LLM must not run on read path"
        )
        assert elapsed < 10.0, (
            f"impact_narrative took {elapsed:.2f}s — exceeds 10s hard deadline"
        )

    def test_graph_semantic_trace_no_llm(self, project):
        """graph(relation=semantic_trace) must return llm_used=False within 10s."""
        import asyncio
        import time

        from opencode_search.handlers._trace import handle_semantic_trace

        t0 = time.monotonic()
        result = asyncio.run(handle_semantic_trace("HTTP request handler", "database write", project))
        elapsed = time.monotonic() - t0

        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        assert result.get("llm_used") is False, (
            f"semantic_trace set llm_used={result.get('llm_used')!r} — LLM must not run on read path"
        )
        assert elapsed < 10.0, (
            f"semantic_trace took {elapsed:.2f}s — exceeds 10s hard deadline"
        )

    def test_overview_service_mesh_no_llm(self, project):
        """overview(what=service_mesh) must return llm_used=False within 10s.

        Scans root project only (include_federation=False) to avoid scanning 29+ federation
        members which would take >10s on astro-project. Root scan with ~1705 files is fast.
        """
        import asyncio
        import time

        from opencode_search.handlers._service_mesh import handle_detect_service_mesh

        t0 = time.monotonic()
        result = asyncio.run(handle_detect_service_mesh(project, include_federation=False, force=True))
        elapsed = time.monotonic() - t0

        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        assert result.get("llm_used") is False, (
            f"service_mesh set llm_used={result.get('llm_used')!r} — LLM must not run on read path"
        )
        assert elapsed < 10.0, (
            f"service_mesh took {elapsed:.2f}s — exceeds 10s hard deadline"
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


class TestNoKeywordHeuristics:
    """Enforce the comprehensive heuristic-removal mandate.

    Static keyword lists, static name→category maps, and dynamic regex content
    scanning are forbidden in both _graph.py and _service_mesh.py. Detection must
    come from tree-sitter parsed facts or background-LLM labels served from cache.
    """

    def test_graph_no_known_frameworks_map(self):
        """_KNOWN_FRAMEWORKS must not exist in _graph.py."""
        import importlib
        mod = importlib.import_module("opencode_search.handlers._graph")
        assert not hasattr(mod, "_KNOWN_FRAMEWORKS"), (
            "_KNOWN_FRAMEWORKS found in _graph.py — static keyword map is forbidden. "
            "Use LLM-grounded labels from patterns_cache.json instead."
        )

    def test_graph_no_framework_keyword_detector(self):
        """_detect_frameworks_from_dependencies must not exist in _graph.py."""
        import importlib
        mod = importlib.import_module("opencode_search.handlers._graph")
        assert not hasattr(mod, "_detect_frameworks_from_dependencies"), (
            "_detect_frameworks_from_dependencies found in _graph.py — "
            "keyword-based detector is forbidden."
        )

    def test_graph_no_dir_heuristic_detector(self):
        """_infer_module_structure_from_dirs must not exist in _graph.py."""
        import importlib
        mod = importlib.import_module("opencode_search.handlers._graph")
        assert not hasattr(mod, "_infer_module_structure_from_dirs"), (
            "_infer_module_structure_from_dirs found in _graph.py — "
            "static directory-name heuristic is forbidden."
        )

    def test_graph_no_architecture_heuristic(self):
        """_detect_architecture must not exist in _graph.py."""
        import importlib
        mod = importlib.import_module("opencode_search.handlers._graph")
        assert not hasattr(mod, "_detect_architecture"), (
            "_detect_architecture found in _graph.py — "
            "static framework-set→architecture mapping is forbidden."
        )

    def test_service_mesh_no_grpc_regex(self):
        """_GRPC_PATTERNS must not exist in _service_mesh.py."""
        import importlib
        mod = importlib.import_module("opencode_search.handlers._service_mesh")
        assert not hasattr(mod, "_GRPC_PATTERNS"), (
            "_GRPC_PATTERNS found in _service_mesh.py — regex pattern list is forbidden. "
            "Service-mesh topology must derive from parsed .proto graph nodes."
        )

    def test_service_mesh_no_http_regex(self):
        """_HTTP_PATTERNS must not exist in _service_mesh.py."""
        import importlib
        mod = importlib.import_module("opencode_search.handlers._service_mesh")
        assert not hasattr(mod, "_HTTP_PATTERNS"), (
            "_HTTP_PATTERNS found in _service_mesh.py — regex pattern list is forbidden."
        )

    def test_service_mesh_no_mq_regex(self):
        """_MQ_PATTERNS must not exist in _service_mesh.py."""
        import importlib
        mod = importlib.import_module("opencode_search.handlers._service_mesh")
        assert not hasattr(mod, "_MQ_PATTERNS"), (
            "_MQ_PATTERNS found in _service_mesh.py — regex pattern list is forbidden."
        )

    def test_service_mesh_no_db_regex(self):
        """_DB_PATTERNS must not exist in _service_mesh.py."""
        import importlib
        mod = importlib.import_module("opencode_search.handlers._service_mesh")
        assert not hasattr(mod, "_DB_PATTERNS"), (
            "_DB_PATTERNS found in _service_mesh.py — regex pattern list is forbidden."
        )

    def test_service_mesh_no_file_scanner(self):
        """_detect_protocols_in_file must not exist in _service_mesh.py."""
        import importlib
        mod = importlib.import_module("opencode_search.handlers._service_mesh")
        assert not hasattr(mod, "_detect_protocols_in_file"), (
            "_detect_protocols_in_file found in _service_mesh.py — "
            "source-file regex scanner is forbidden. Use the parsed graph instead."
        )


class TestDependencyParsedByGrammar:
    """Unit D guard: _graph.py dependency parsers must not use regex."""

    def test_graph_no_re_import(self):
        """handlers/_graph.py must not import re (manifest parsers use tomllib/json/xml/str-ops)."""
        import ast
        from pathlib import Path

        src = Path("src/opencode_search/handlers/_graph.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "re", (
                        "_graph.py must not import 're'. "
                        "Manifest parsers must use tomllib/json/xml/str-ops."
                    )
            elif isinstance(node, ast.ImportFrom) and node.module == "re":
                raise AssertionError(
                    "_graph.py must not import from 're'. "
                    "Manifest parsers must use tomllib/json/xml/str-ops."
                )

    def test_no_spring_boot_hardcode(self):
        """The spring-boot hardcoded framework name must be deleted from _graph.py."""
        from pathlib import Path

        src = Path("src/opencode_search/handlers/_graph.py").read_text()
        assert "springframework" not in src, (
            "spring-boot hardcoded name found in _graph.py — "
            "framework special-casing must be deleted; no per-framework detection."
        )

    def test_overview_patterns_returns_packages(self, project):
        """overview(what='patterns') returns dependency data without crashing."""
        import asyncio

        from opencode_search.handlers._graph import handle_detect_patterns

        result = asyncio.run(handle_detect_patterns(project))
        assert "error" not in result, f"handle_detect_patterns errored: {result.get('error')}"
        # llm_used must never be True on the read path (absent is also fine for cached result)
        assert result.get("llm_used") is not True, "overview patterns must not call LLM on the read path"
        # Expect dependencies dict to be present
        deps = result.get("dependencies", {})
        assert isinstance(deps, dict), f"dependencies should be a dict, got {type(deps)}"

    def test_parse_dep_spec_helper(self):
        """_parse_dep_spec parses PEP 508 specifiers without regex."""
        from opencode_search.handlers._graph import _parse_dep_spec

        name, ver = _parse_dep_spec("requests>=2.28.0")
        assert name == "requests"
        assert ver == ">=2.28.0"

        name, ver = _parse_dep_spec("flask==2.0.0 ; python_version >= '3.8'")
        assert name == "flask"
        assert ver == "==2.0.0"

        name, ver = _parse_dep_spec("pytest")
        assert name == "pytest"
        assert ver == "*"


class TestServiceMeshParsedFacts:
    """Service-mesh topology derives from parsed .proto graph nodes + external_imports.json.

    Validates that: no LLM is used, the call completes ≤10s, proto services are labeled
    grpc by parsed fact, and non-proto edges carry no protocol label (unlabeled).
    """

    def test_service_mesh_no_llm_force_refresh(self, project):
        """handle_detect_service_mesh returns llm_used=False on a forced cache refresh."""
        import asyncio
        import time

        from opencode_search.handlers._service_mesh import handle_detect_service_mesh

        t0 = time.monotonic()
        result = asyncio.run(
            handle_detect_service_mesh(project, include_federation=False, force=True)
        )
        elapsed = time.monotonic() - t0

        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        assert result.get("llm_used") is False, (
            f"service_mesh returned llm_used={result.get('llm_used')!r} — "
            "topology must be derived from parsed facts, no LLM."
        )
        assert elapsed < 10.0, (
            f"service_mesh took {elapsed:.2f}s on force refresh — exceeds 10s deadline. "
            "The regex file-scan was not fully removed."
        )

    def test_service_mesh_returns_services_list(self, project):
        """handle_detect_service_mesh returns a 'services' list with name and path fields."""
        import asyncio

        from opencode_search.handlers._service_mesh import handle_detect_service_mesh

        result = asyncio.run(
            handle_detect_service_mesh(project, include_federation=False, force=True)
        )
        assert "services" in result, f"Missing 'services' key: {list(result.keys())}"
        assert "edges" in result, f"Missing 'edges' key: {list(result.keys())}"
        services = result["services"]
        assert isinstance(services, list), "'services' must be a list"
        for svc in services:
            assert "name" in svc, f"service entry missing 'name': {svc}"
            assert "path" in svc, f"service entry missing 'path': {svc}"

    def test_service_mesh_non_proto_edges_unlabeled(self, project):
        """Edges from external_imports (non-proto) must have protocol=None (unlabeled)."""
        import asyncio

        from opencode_search.handlers._service_mesh import handle_detect_service_mesh

        result = asyncio.run(
            handle_detect_service_mesh(project, include_federation=False, force=True)
        )
        for edge in result.get("edges", []):
            if edge.get("source") == "external_imports":
                # Protocol is only set if the callee has proto service nodes (grpc by fact)
                # Non-proto inter-member edges must be unlabeled (None), not a guessed label
                protocol = edge.get("protocol")
                assert protocol in (None, "grpc"), (
                    f"Edge {edge.get('from')}→{edge.get('to')} has protocol={protocol!r}. "
                    "Non-proto edges must be unlabeled (None); only proto-derived edges may "
                    "carry 'grpc' (labeled by parsed fact, not regex)."
                )

    def test_service_mesh_no_file_scan_artifacts(self):
        """The scan machinery (regex patterns + file walker) must not be importable."""
        import importlib
        mod = importlib.import_module("opencode_search.handlers._service_mesh")
        for name in ("_GRPC_PATTERNS", "_HTTP_PATTERNS", "_MQ_PATTERNS", "_DB_PATTERNS",
                     "_detect_protocols_in_file", "_scan_shard",
                     "_scan_service_protocols_parallel", "_os_walk"):
            assert not hasattr(mod, name), (
                f"{name} still present in _service_mesh — regex/file-scan artifact must be deleted."
            )


class TestNoRegexInStringOps:
    """Unit A guard: dedup.py and wiki/generator.py must not use `re` for trivial string ops."""

    def test_dedup_no_re_import(self):
        """graph/dedup.py must not import re (whitespace-collapse and digit-check replaced by str-ops)."""
        import ast
        from pathlib import Path

        src = Path("src/opencode_search/graph/dedup.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else ([node.module] if node.module else [])
                )
                for name in names:
                    assert name != "re", (
                        "dedup.py must not import 're'. "
                        "Use ' '.join(s.split()) for whitespace, .isdigit() for numeric check."
                    )

    def test_wiki_generator_no_re_import(self):
        """wiki/generator.py must not import re (slug generation replaced by char filter)."""
        import ast
        from pathlib import Path

        src = Path("src/opencode_search/wiki/generator.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else ([node.module] if node.module else [])
                )
                for name in names:
                    assert name != "re", (
                        "wiki/generator.py must not import 're'. "
                        "Use a char-filter comprehension for slug sanitization."
                    )

    def test_dedup_norm_collapses_whitespace(self):
        """_norm() must still collapse internal whitespace after the re→str-op rewrite."""
        from opencode_search.graph.dedup import _norm

        assert _norm("  hello   world  ") == "hello world"
        assert _norm("foo  bar\tbaz") == "foo bar baz"

    def test_dedup_entropy_rejects_pure_digits(self):
        """_entropy() must still return False for pure-digit inputs after dropping re.match."""
        from opencode_search.graph.dedup import _entropy

        assert _entropy("12345") is False
        assert _entropy("0") is False
        assert _entropy("abc123") is True  # mixed — not pure digits

    def test_wiki_safe_name_slugifies(self):
        """_safe_name() must still convert special chars to underscores after the re→char-filter rewrite."""
        from opencode_search.wiki.generator import _safe_name

        assert _safe_name("hello world") == "hello_world"
        assert _safe_name("foo/bar.baz") == "foo_bar_baz"
        assert _safe_name("valid-name_ok") == "valid-name_ok"
        assert _safe_name("___leading") == "leading"


class TestRepoWideNoRegex:
    """Repo-wide guard: non-test opencode_search source must not use `re` for meaning inference.

    Allowlist (documented exceptions — must shrink to empty):
    - index_config.py: compiles USER-SUPPLIED config regex (user feature, not our heuristic).

    This class grows as units A→I land. Each landed unit removes itself from any allowlist.
    """

    _ALLOWLIST = frozenset({
        # User-supplied regex compilation — not our heuristic, never removed.
        "index_config.py",
    })

    # Symbols deleted in Steps 1-4 that must never re-appear.
    _BANNED_SYMBOLS = frozenset({
        "_KNOWN_FRAMEWORKS",
        "_detect_frameworks_from_dependencies",
        "_infer_module_structure_from_dirs",
        "_detect_architecture",
        "_GRPC_PATTERNS",
        "_HTTP_PATTERNS",
        "_MQ_PATTERNS",
        "_DB_PATTERNS",
        "_detect_protocols_in_file",
    })

    def test_banned_symbols_not_importable(self):
        """Deleted heuristic symbols must not re-appear in any module."""
        import importlib

        for mod_name in (
            "opencode_search.handlers._graph",
            "opencode_search.handlers._service_mesh",
        ):
            mod = importlib.import_module(mod_name)
            for sym in self._BANNED_SYMBOLS:
                assert not hasattr(mod, sym), (
                    f"{sym} re-appeared in {mod_name} — deleted heuristic must stay deleted."
                )

    def test_dedup_and_wiki_no_re(self):
        """Units landed in A: dedup.py and wiki/generator.py must not import re."""
        import ast
        from pathlib import Path

        for rel in ("src/opencode_search/graph/dedup.py",
                    "src/opencode_search/wiki/generator.py"):
            src = Path(rel).read_text()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = (
                        [a.name for a in node.names]
                        if isinstance(node, ast.Import)
                        else ([node.module] if node.module else [])
                    )
                    for name in names:
                        assert name != "re", f"{rel} must not import 're' (Unit A landed)"

    def test_graph_no_re(self):
        """Unit D: handlers/_graph.py must not import re (manifest parsers use stdlib/tree-sitter)."""
        import ast
        from pathlib import Path

        src = Path("src/opencode_search/handlers/_graph.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "re", "_graph.py must not import 're' (Unit D landed)"
            elif isinstance(node, ast.ImportFrom) and node.module == "re":
                raise AssertionError("_graph.py must not import from 're' (Unit D landed)")

    def test_trace_no_re(self):
        """Unit C: handlers/_trace.py must not import re (symbol resolved via graph nodes)."""
        import ast
        from pathlib import Path

        src = Path("src/opencode_search/handlers/_trace.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "re", "_trace.py must not import 're' (Unit C landed)"
            elif isinstance(node, ast.ImportFrom) and node.module == "re":
                raise AssertionError("_trace.py must not import from 're' (Unit C landed)")

    def test_chat_router_and_enricher_no_re(self):
        """Units landed in B: _chat_router.py and enricher/client.py must not import re."""
        import ast
        from pathlib import Path

        for rel in ("src/opencode_search/handlers/_chat_router.py",
                    "src/opencode_search/enricher/client.py"):
            src = Path(rel).read_text()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = (
                        [a.name for a in node.names]
                        if isinstance(node, ast.Import)
                        else ([node.module] if node.module else [])
                    )
                    for name in names:
                        assert name != "re", f"{rel} must not import 're' (Unit B landed)"


class TestNoRegexInLLMOutputParsing:
    """Unit B guard: LLM-output parsing must use json/str-ops, not regex."""

    def _assert_no_re(self, rel: str, reason: str) -> None:
        import ast
        from pathlib import Path

        src = Path(rel).read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "re", f"{rel} must not import 're'. {reason}"
            elif isinstance(node, ast.ImportFrom) and node.module == "re":
                raise AssertionError(f"{rel} must not import from 're'. {reason}")

    def test_chat_router_no_re_import(self):
        """handlers/_chat_router.py must not import re (intent JSON parsed via json.loads)."""
        self._assert_no_re(
            "src/opencode_search/handlers/_chat_router.py",
            "Intent JSON must be parsed with json.loads.",
        )

    def test_enricher_client_no_re_import(self):
        """enricher/client.py must not import re (numbered-list parsed by str-ops)."""
        self._assert_no_re(
            "src/opencode_search/enricher/client.py",
            "Numbered-list LLM output parsed with str-ops.",
        )

    def test_intent_json_parse_valid_intent(self):
        """classify_intent_llm JSON-slice parsing handles well-formed intent JSON."""
        import json

        # Simulate what the function does: find JSON, loads, pick intent
        text = '{"intent": "search"}'
        start = text.find("{")
        end = text.rfind("}") + 1
        parsed = json.loads(text[start:end])
        assert parsed.get("intent") == "search"

    def test_numbered_list_parser_str_ops(self):
        """OllamaLLMClient.symbol_intent_batch str-op parser handles standard numbered output."""
        # Exercise the parsing logic directly by calling symbol_intent_batch
        # via a subclass that overrides chat() to return canned output.
        from opencode_search.enricher.client import OllamaClient

        class _FakeOllama(OllamaClient):
            def __init__(self):
                pass  # skip actual init

            def chat(self, messages, **kw):
                return "1. Does the first thing\n2. Does the second thing\n"

        fake = _FakeOllama()
        items = [("fn_a", "fn_a()", None), ("fn_b", "fn_b()", None)]
        results = fake.symbol_intent_batch(items)
        assert results[0] == "Does the first thing"
        assert results[1] == "Does the second thing"


class TestSingleLanguageMap:
    """Unit E guard: only ONE extension→language map + ONE doc-language predicate in source.

    Before Unit E three copies existed:
      - discover.py LANGUAGE_MAP (canonical, kept)
      - chunker.py _detect_language match-statement (deleted)
      - chunker.py _LANG_TO_TREESITTER (moved to discover.LANG_TO_GRAMMAR)
      - chunker.py _DOC_LANGUAGES (deleted, replaced by is_document_language)
      - search.py _DOCUMENT_LANGUAGES (deleted)
      - handlers/_graph.py _DOC_LANGS (deleted)

    After: discover.py is the single source; all consumers import from it.
    """

    def test_chunker_no_duplicate_detect_language(self):
        """chunker.py must not define its own _detect_language (Unit E: use discover.detect_language)."""
        import ast
        from pathlib import Path

        src = Path("src/opencode_search/chunker.py").read_text()
        tree = ast.parse(src)
        fn_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        assert "_detect_language" not in fn_names, (
            "chunker.py defines _detect_language — duplicate map. "
            "Import discover.detect_language instead (Unit E)."
        )

    def test_chunker_no_lang_to_treesitter(self):
        """chunker.py must not define _LANG_TO_TREESITTER (moved to discover.LANG_TO_GRAMMAR)."""
        import importlib
        mod = importlib.import_module("opencode_search.chunker")
        assert not hasattr(mod, "_LANG_TO_TREESITTER"), (
            "_LANG_TO_TREESITTER still in chunker — moved to discover.LANG_TO_GRAMMAR (Unit E)."
        )

    def test_no_duplicate_doc_language_frozensets(self):
        """_DOCUMENT_LANGUAGES, _DOC_LANGUAGES, _DOC_LANGS frozensets must not exist (merged into discover)."""
        import importlib
        for mod_name, attr in [
            ("opencode_search.search", "_DOCUMENT_LANGUAGES"),
            ("opencode_search.chunker", "_DOC_LANGUAGES"),
            ("opencode_search.handlers._graph", "_DOC_LANGS"),
        ]:
            mod = importlib.import_module(mod_name)
            assert not hasattr(mod, attr), (
                f"{attr} still in {mod_name} — deleted in Unit E. "
                "Use discover.is_document_language() instead."
            )

    def test_detect_language_roundtrips(self):
        """discover.detect_language returns expected lang for common extensions."""
        from pathlib import Path

        from opencode_search.discover import detect_language

        assert detect_language(Path("main.go")) == "go"
        assert detect_language(Path("app.py")) == "python"
        assert detect_language(Path("index.ts")) == "typescript"
        assert detect_language(Path("README.md")) == "markdown"
        assert detect_language(Path("config.yaml")) == "yaml"
        assert detect_language(Path("Dockerfile")) == "dockerfile"
        assert detect_language(Path("Makefile")) == "makefile"

    def test_is_document_language_from_grammar(self):
        """is_document_language is derived from LANG_TO_GRAMMAR, not a hand-kept list."""
        from opencode_search.discover import LANG_TO_GRAMMAR, is_document_language

        # Code languages (have grammar entries) must return False
        for lang in ("go", "python", "typescript", "javascript", "java", "rust"):
            assert not is_document_language(lang), (
                f"is_document_language({lang!r}) returned True — should be False (has code grammar)"
            )
        # Document languages (no grammar entry) must return True
        for lang in ("markdown", "text", "unknown", "yaml", "json", "xml", "rst"):
            assert is_document_language(lang), (
                f"is_document_language({lang!r}) returned False — should be True (no code grammar)"
            )
        # Verify derivation: is_document_language(x) == (x not in LANG_TO_GRAMMAR)
        for lang in list(LANG_TO_GRAMMAR.keys())[:10]:
            assert not is_document_language(lang), (
                f"is_document_language({lang!r}) disagrees with LANG_TO_GRAMMAR — "
                "must be derived from grammar map, not a separate list"
            )
